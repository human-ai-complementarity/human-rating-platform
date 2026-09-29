"""Completion signals across independent listeners, using real PostgreSQL."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from models import AssistancePreparation, Question, Rater
from services.assistance.base import InteractionStep, StepType
from services.assistance.preparation import PreparationContext, QuestionSnapshot
from services.assistance.registry import get_method
from services.assistance.runner import PreparationRunner
from test_preparation_runner import setup_rater


@pytest.mark.parametrize("finish", ["complete", "delete", "reconnect"])
def test_waiters_use_signals_without_polling(client, monkeypatch, finish):
    session, _, question = setup_rater(client)

    async def scenario():
        producer = client.app.state.preparation_runner
        observer = PreparationRunner(producer.database)
        # An independent API listener receiving notifications from the producer.
        listener = asyncio.create_task(observer._listen())
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def prepare(*args, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return InteractionStep(type=StepType.DISPLAY, is_terminal=True)

        monkeypatch.setattr("services.assistance.methods.top_n.TopNAssistance.start", prepare)
        session_factory = producer.database.session
        reads = 0

        @asynccontextmanager
        async def count_reads():
            nonlocal reads
            if asyncio.current_task().get_name().startswith("test-waiter"):
                reads += 1
            async with session_factory() as db:
                yield db

        monkeypatch.setattr(producer.database, "session", count_reads)
        tasks = []
        try:
            await asyncio.wait_for(observer._listening.wait(), 5)
            async with producer.database.session() as db:
                rater = await db.get(Rater, session["rater_id"])
                q = await db.get(Question, question["id"])
                spec = get_method("top_n").plan_preparation(
                    PreparationContext(QuestionSnapshot.capture(q), "{}")
                )
                identifier = await producer.ensure(
                    rater_id=rater.id,
                    question_id=q.id,
                    session_start=rater.session_start,
                    method_name="top_n",
                    spec=spec,
                    params={},
                    deadline_at=datetime.now(UTC) + timedelta(minutes=5),
                    demanded=True,
                )
            await asyncio.wait_for(entered.wait(), 5)
            tasks = [
                asyncio.create_task(observer.wait(identifier), name=f"test-waiter-{i}")
                for i in range(20)
            ]
            # Let each request perform its initial read, then verify that time
            # passing causes no additional database work for waiting requests.
            async with asyncio.timeout(5):
                while reads < len(tasks):
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            initial_reads = reads
            await asyncio.sleep(0.35)
            assert reads == initial_reads == 20
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            if finish == "reconnect":
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            if finish == "delete":
                async with producer.database.session() as db:
                    await db.execute(
                        delete(AssistancePreparation).where(AssistancePreparation.id == identifier)
                    )
                    await db.commit()
            release.set()
            if finish == "reconnect":
                # Finish while this listener is absent. LISTEN-before-read on
                # reconnect must recover a notification it could not receive.
                async with asyncio.timeout(5):
                    while True:
                        async with producer.database.session() as db:
                            row = await db.get(AssistancePreparation, identifier)
                            if row.status == "complete":
                                break
                        await asyncio.sleep(0.01)
                listener = asyncio.create_task(observer._listen())
            results = await asyncio.wait_for(asyncio.gather(*tasks[1:], return_exceptions=True), 5)
            if finish == "delete":
                assert all(
                    isinstance(result, HTTPException) and result.status_code == 409
                    for result in results
                )
            else:
                assert len({result.id for result in results}) == 1
                assert all(result.step_type == StepType.DISPLAY for result in results)
                # A request arriving after completion needs no new signal.
                assert (await observer.wait(identifier)).id == results[0].id
            assert calls == 1
            assert observer._waiters == {}
        finally:
            release.set()
            listener.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(listener, *tasks, return_exceptions=True)

    client.portal.call(scenario)


def test_invalid_claims_do_not_delay_work_behind_them(client, monkeypatch):
    from unittest.mock import AsyncMock

    session, _, question = setup_rater(client)
    start = AsyncMock(return_value=InteractionStep(type=StepType.NONE, is_terminal=True))
    monkeypatch.setattr("services.assistance.methods.top_n.TopNAssistance.start", start)

    async def scenario():
        runner = client.app.state.preparation_runner
        await runner.close()
        async with runner.database.session() as db:
            rater = await db.get(Rater, session["rater_id"])
            q = await db.get(Question, question["id"])
            spec = get_method("top_n").plan_preparation(
                PreparationContext(QuestionSnapshot.capture(q), "{}")
            )
        for seconds_ago in [3, 2, 1, 0]:
            identifier = await runner.ensure(
                rater_id=rater.id,
                question_id=q.id,
                session_start=rater.session_start - timedelta(seconds=seconds_ago),
                method_name="top_n",
                spec=spec,
                params={},
                deadline_at=datetime.now(UTC) + timedelta(minutes=5),
                demanded=True,
            )
        runner.start()
        result = await asyncio.wait_for(runner.wait(identifier), 5)
        assert result.step_type == StepType.NONE
        assert start.await_count == 1

    client.portal.call(scenario)
