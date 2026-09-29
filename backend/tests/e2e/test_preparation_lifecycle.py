"""Real claims and computation across runner failure/recovery boundaries."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from models import AssistancePreparation, Rater
from services.assistance.base import InteractionStep, StepType
from services.assistance.llm import provider_slot
from services.assistance.preparation import PreparationSpec
from services.assistance.runner import PreparationRunner
from test_preparation_runner import setup_rater


async def enqueue(runner, rater_id, question_id):
    async with runner.database.session() as db:
        rater = await db.get(Rater, rater_id)
    return await runner.ensure(
        rater_id=rater.id,
        question_id=question_id,
        session_start=rater.session_start,
        method_name="top_n",
        spec=PreparationSpec("initial_step", 1, "{}"),
        params={},
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        demanded=True,
    )


def test_listener_startup_is_bounded_and_disconnect_is_retryable(client, monkeypatch):
    async def scenario():
        runner = client.app.state.preparation_runner
        await runner.close()
        connection_factory = runner.database.notification_connection

        @asynccontextmanager
        async def unavailable():
            raise OSError("listener unavailable")
            yield  # pragma: no cover

        monkeypatch.setattr(runner.database, "notification_connection", unavailable)
        monkeypatch.setattr("services.assistance.runner.STARTUP_SECONDS", 0.05)
        runner.start()
        with pytest.raises(RuntimeError, match="startup timeout"):
            await runner.wait_ready()
        assert not runner.ready
        with pytest.raises(HTTPException) as error:
            await runner.wait(123)
        assert error.value.status_code == 503
        await runner.close()
        assert not runner._tasks
        monkeypatch.setattr(runner.database, "notification_connection", connection_factory)
        monkeypatch.setattr("services.assistance.runner.STARTUP_SECONDS", 5)
        runner.start()
        await runner.wait_ready()
        assert runner.ready
        runner._tasks[0].cancel()
        await asyncio.gather(runner._tasks[0], return_exceptions=True)
        assert not runner.ready

    client.portal.call(scenario)
    assert client.get("/api/health").status_code == 503


@pytest.mark.parametrize("status", ["preparing", "consuming"])
@pytest.mark.parametrize("successor", [False, True])
def test_shutdown_releases_only_owned_claims_without_losing_progress(client, status, successor):
    session, _, question = setup_rater(client)

    async def scenario():
        runner = client.app.state.preparation_runner
        await runner.close()
        identifier = await enqueue(runner, session["rater_id"], question["id"])
        if status == "consuming":
            async with runner.database.session() as db:
                row = await db.get(AssistancePreparation, identifier)
                row.status = "ready"
                row.artifact_json = '{"saved":true}'
                await db.commit()
        claim = await runner._claim(foreground_only=True)
        if successor:
            async with runner.database.session() as db:
                row = await db.get(AssistancePreparation, identifier)
                row.owner_token = "successor-token"
                await db.commit()
        await runner.close()
        async with runner.database.session() as db:
            row = await db.get(AssistancePreparation, identifier)
            assert row.attempts == claim.attempts
            assert row.artifact_json == claim.artifact_json
            if successor:
                assert row.owner_token == "successor-token"
                assert row.status == status
                assert row.claim_expires_at == claim.claim_expires_at
            else:
                assert row.owner_token is None
                assert row.claim_expires_at is None
                assert row.status == ("ready" if status == "consuming" else "queued")
        if not successor:
            replacement = PreparationRunner(runner.database)
            recovered = await replacement._claim(foreground_only=True)
            assert recovered.id == identifier
            assert recovered.owner_token != claim.owner_token
            await replacement.close()

    client.portal.call(scenario)


def test_shutdown_cancels_running_computation_before_releasing_claim(client, monkeypatch):
    session, _, question = setup_rater(client)

    async def scenario():
        runner = client.app.state.preparation_runner
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def prepare(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        method = AsyncMock()
        method.prepare.side_effect = prepare
        monkeypatch.setattr("services.assistance.runner.get_method", lambda _: method)
        identifier = await enqueue(runner, session["rater_id"], question["id"])
        await asyncio.wait_for(entered.wait(), 5)
        await runner.close()
        assert cancelled.is_set()
        async with runner.database.session() as db:
            row = await db.get(AssistancePreparation, identifier)
            assert row.status == "queued"
            assert row.owner_token is None
            assert row.attempts == 1

    client.portal.call(scenario)


def test_expired_execution_immediately_reclaims_without_recovery_sleep(client, monkeypatch):
    session, _, question = setup_rater(client)

    async def scenario():
        runner = client.app.state.preparation_runner
        await runner.close()
        identifier = await enqueue(runner, session["rater_id"], question["id"])
        entered, release, retried = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def prepare(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            else:
                retried.set()
            return {}

        method = AsyncMock()
        method.prepare.side_effect = prepare
        method.consume_preparation.return_value = InteractionStep(
            type=StepType.NONE, is_terminal=True
        )
        monkeypatch.setattr("services.assistance.runner.get_method", lambda _: method)
        runner._listening.set()
        worker = asyncio.create_task(runner._loop(foreground_only=True, wake=asyncio.Event()))
        runner._tasks = [worker]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with runner.database.session() as db:
                row = await db.get(AssistancePreparation, identifier)
                row.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await db.commit()
            release.set()
            # No listener and no notification can wake this worker. A normal
            # expired-publication return must continue directly to another claim.
            await asyncio.wait_for(retried.wait(), 2)
        finally:
            await runner.close()

    client.portal.call(scenario)


def test_foreground_burst_uses_eight_workers_and_bounded_provider_fanout(client, monkeypatch):
    session, _, question = setup_rater(client)

    async def scenario():
        runner = client.app.state.preparation_runner
        all_workers_entered = asyncio.Event()
        active_jobs = peak_jobs = active_calls = peak_calls = total_calls = 0

        async def provider_call():
            nonlocal active_calls, peak_calls, total_calls
            async with provider_slot():
                active_calls += 1
                total_calls += 1
                peak_calls = max(peak_calls, active_calls)
                await asyncio.sleep(0.005)
                active_calls -= 1

        async def prepare(_):
            nonlocal active_jobs, peak_jobs
            active_jobs += 1
            peak_jobs = max(peak_jobs, active_jobs)
            if active_jobs == 8:
                all_workers_entered.set()
            await all_workers_entered.wait()
            await asyncio.gather(*(provider_call() for _ in range(4)))
            active_jobs -= 1
            return {}

        method = AsyncMock()
        method.prepare.side_effect = prepare
        method.consume_preparation.return_value = InteractionStep(
            type=StepType.DISPLAY, is_terminal=True
        )
        monkeypatch.setattr("services.assistance.runner.get_method", lambda _: method)
        async with runner.database.session() as db:
            original = await db.get(Rater, session["rater_id"])
            raters = [
                Rater(
                    prolific_id=f"burst-{i}",
                    experiment_id=original.experiment_id,
                    session_start=original.session_start,
                    is_active=True,
                )
                for i in range(24)
            ]
            db.add_all(raters)
            await db.commit()
        identifiers = [await enqueue(runner, r.id, question["id"]) for r in raters]
        results = await asyncio.wait_for(asyncio.gather(*(runner.wait(i) for i in identifiers)), 15)
        assert len({result.id for result in results}) == 24
        assert all(result.step_type == StepType.DISPLAY for result in results)
        assert peak_jobs == 8
        assert peak_calls == 8
        assert total_calls == 96
        assert all(
            result.params is None and result.payload is None and result.state is None
            for result in results
        )

    client.portal.call(scenario)


def test_preview_reset_locks_before_reading_sessions(client, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from config import get_settings
    from models import AssistanceSession
    from services.rater.operations import start_session

    session, _, question = setup_rater(client)

    async def scenario():
        runner = client.app.state.preparation_runner
        await runner.close()
        async with runner.database.session() as db:
            rater = await db.get(Rater, session["rater_id"])
            rater.is_preview = True
            await db.commit()
        identifier = await enqueue(runner, rater.id, question["id"])
        async with runner.database.session() as db:
            row = await db.get(AssistancePreparation, identifier)
            row.artifact_json = "{}"
            row.status = "ready"
            await db.commit()
        claim = await runner._claim(foreground_only=True)
        method = AsyncMock()
        method.consume_preparation.return_value = InteractionStep(
            type=StepType.DISPLAY, is_terminal=True
        )
        monkeypatch.setattr("services.assistance.runner.get_method", lambda _: method)
        sessions_read, release_reset, publication_started = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        execute = AsyncSession.execute

        async def intercept(db, statement, *args, **kwargs):
            task_name = asyncio.current_task().get_name()
            entities = [
                column.get("entity") for column in getattr(statement, "column_descriptions", [])
            ]
            if task_name == "test-publication" and Rater in entities:
                publication_started.set()
            result = await execute(db, statement, *args, **kwargs)
            if task_name == "test-preview-reset" and AssistanceSession in entities:
                sessions_read.set()
                await release_reset.wait()
            return result

        monkeypatch.setattr(AsyncSession, "execute", intercept)

        async def reset():
            async with runner.database.session() as db:
                await start_session(
                    settings=get_settings(),
                    experiment_id=rater.experiment_id,
                    prolific_pid=rater.prolific_id,
                    study_id="preview",
                    session_id="preview",
                    is_preview=True,
                    db=db,
                )

        resetting = asyncio.create_task(reset(), name="test-preview-reset")
        publishing = None
        try:
            await asyncio.wait_for(sessions_read.wait(), 5)
            publishing = asyncio.create_task(runner._execute(claim), name="test-publication")
            await asyncio.wait_for(publication_started.wait(), 5)
            # The reset has already read the session list. Without its rater
            # lock, publication could insert a session that reset never deletes.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(publishing), 0.1)
            release_reset.set()
            await asyncio.wait_for(asyncio.gather(resetting, publishing), 5)
            async with runner.database.session() as db:
                assert (await db.execute(select(AssistanceSession))).scalars().all() == []
                assert (await db.get(AssistancePreparation, identifier)).status == "cancelled"
        finally:
            release_reset.set()
            tasks = [resetting] + ([publishing] if publishing else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    client.portal.call(scenario)


def test_short_http_wait_reattaches_without_restarting_computation(client, monkeypatch):
    session, _, question = setup_rater(client)

    async def scenario():
        runner = client.app.state.preparation_runner
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(_):
            entered.set()
            await release.wait()
            return {}

        method = AsyncMock()
        method.prepare.side_effect = prepare
        method.consume_preparation.return_value = InteractionStep(
            type=StepType.DISPLAY, is_terminal=True
        )
        monkeypatch.setattr("services.assistance.runner.get_method", lambda _: method)
        monkeypatch.setattr("services.assistance.runner.WAIT_SECONDS", 0.05)
        identifier = await enqueue(runner, session["rater_id"], question["id"])
        await asyncio.wait_for(entered.wait(), 5)
        try:
            with pytest.raises(HTTPException) as error:
                await runner.wait(identifier)
            assert error.value.status_code == 503
            # Retry reuses durable demand; the timed-out HTTP waiter did not
            # cancel the running provider computation or consume an attempt.
            assert await enqueue(runner, session["rater_id"], question["id"]) == identifier
            async with runner.database.session() as db:
                row = await db.get(AssistancePreparation, identifier)
                assert row.status == "preparing"
                assert row.attempts == 1
            monkeypatch.setattr("services.assistance.runner.WAIT_SECONDS", 5)
            release.set()
            result = await runner.wait(identifier)
            assert result.step_type == StepType.DISPLAY
            assert method.prepare.await_count == 1
        finally:
            release.set()

    client.portal.call(scenario)
