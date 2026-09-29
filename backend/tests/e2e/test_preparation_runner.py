"""Real database tests for duplicate demand and fenced publication."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from models import AssistancePreparation, AssistanceSession, Rater
from services.assistance.base import InteractionStep, StepType
from services.assistance.preparation import PreparationSpec
from test_characterization import (
    _create_experiment,
    _upload_questions,
    _start_session,
    _rater_headers,
)


def setup_rater(client):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    response = client.patch(
        f"/api/admin/experiments/{experiment['id']}",
        json={
            "assistance_method": "top_n",
            "assistance_params": {"assistance_models": {"top_n": "openrouter/test"}},
        },
    )
    assert response.status_code == 200
    session = _start_session(client, experiment["id"])
    headers = _rater_headers(session)
    question = client.get("/api/raters/next-question", headers=headers).json()
    return session, headers, question


@pytest.mark.parametrize("step_type", [StepType.DISPLAY, StepType.NONE])
def test_concurrent_starts_reuse_one_result(client, monkeypatch, step_type):
    _, headers, question = setup_rater(client)

    async def prepare(*args, **kwargs):
        await asyncio.sleep(0.1)
        return InteractionStep(type=step_type, payload={"candidates": []}, is_terminal=True)

    start = AsyncMock(side_effect=prepare)
    monkeypatch.setattr("services.assistance.methods.top_n.TopNAssistance.start", start)

    def call():
        response = client.post(
            "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
        )
        assert response.status_code == 200, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: call(), range(3)))
    assert responses[0] == responses[1] == responses[2] == call()
    assert start.await_count == 1


def test_expired_owner_cannot_publish_and_new_owner_recovers(client, monkeypatch):
    session, _, question = setup_rater(client)

    async def scenario():
        runner = client.app.state.preparation_runner
        await runner.close()
        async with runner.database.session() as db:
            rater = await db.get(Rater, session["rater_id"])
            identifier = await runner.ensure(
                rater_id=rater.id,
                question_id=question["id"],
                session_start=rater.session_start,
                method_name="top_n",
                spec=PreparationSpec("initial_step", 1, "{}"),
                params={},
                deadline_at=datetime.now(UTC) + timedelta(minutes=5),
                demanded=True,
            )
        first = await runner._claim(foreground_only=True)
        async with runner.database.session() as db:
            row = await db.get(AssistancePreparation, identifier)
            row.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        second = await runner._claim(foreground_only=True)
        assert first.owner_token != second.owner_token
        method = AsyncMock()
        method.prepare.return_value = {
            "type": "none",
            "payload": {},
            "state": {},
            "is_terminal": True,
        }
        monkeypatch.setattr("services.assistance.runner.get_method", lambda _: method)
        await runner._execute(first)
        async with runner.database.session() as db:
            row = await db.get(AssistancePreparation, identifier)
            assert row.owner_token == second.owner_token
            assert row.artifact_json is None
        await runner._execute(second)
        async with runner.database.session() as db:
            row = await db.get(AssistancePreparation, identifier)
            assert row.status == "ready"
            assert row.artifact_json is not None
            assert (await db.execute(select(AssistanceSession))).scalars().all() == []

    client.portal.call(scenario)


def test_reset_invalidates_an_inflight_result(client, monkeypatch):
    session, _, question = setup_rater(client)

    async def scenario():
        runner = client.app.state.preparation_runner
        await runner.close()
        async with runner.database.session() as db:
            rater = await db.get(Rater, session["rater_id"])
            identifier = await runner.ensure(
                rater_id=rater.id,
                question_id=question["id"],
                session_start=rater.session_start,
                method_name="top_n",
                spec=PreparationSpec("initial_step", 1, "{}"),
                params={},
                deadline_at=datetime.now(UTC) + timedelta(minutes=5),
                demanded=True,
            )
        claim = await runner._claim(foreground_only=True)
        async with runner.database.session() as db:
            rater = await db.get(Rater, session["rater_id"])
            rater.session_start = datetime.now(UTC)
            await db.commit()
        method = AsyncMock()
        method.prepare.return_value = {}
        monkeypatch.setattr("services.assistance.runner.get_method", lambda _: method)
        await runner._execute(claim)
        async with runner.database.session() as db:
            assert (await db.get(AssistancePreparation, identifier)).status == "cancelled"
            assert (await db.execute(select(AssistanceSession))).scalars().all() == []

    client.portal.call(scenario)


@pytest.mark.parametrize("fails", [False, True])
def test_prepared_start_records_one_event_and_preserves_failed_result(
    client, monkeypatch, sync_engine, fails
):
    from test_assistance_events import _events

    _, headers, question = setup_rater(client)

    async def prepare(*args, **kwargs):
        await asyncio.sleep(0.02)
        if fails:
            raise RuntimeError("controlled preparation failure")
        return InteractionStep(type=StepType.DISPLAY, payload={"candidates": []}, is_terminal=True)

    start = AsyncMock(side_effect=prepare)
    monkeypatch.setattr("services.assistance.methods.top_n.TopNAssistance.start", start)
    first = client.post(
        "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
    )
    assert second.json() == first.json()
    assert first.json()["turn"] == 1
    assert start.await_count == 1
    (event,) = _events(sync_engine, first.json()["session_id"])
    assert event["step_type"] == first.json()["type"]
    assert event["payload"]["response"]["payload"] == first.json()["payload"]
    assert event["payload"]["request"]["retried_step_type"] is None
    assert event["latency_ms"] >= 20
    if fails:
        assert event["error"] == "RuntimeError: controlled preparation failure"
    else:
        assert event["error"] is None


def test_consumption_discards_duplicate_artifact_and_cleanup_is_bounded(
    client, monkeypatch, sync_engine
):
    session, headers, question = setup_rater(client)
    start = AsyncMock(
        return_value=InteractionStep(
            type=StepType.DISPLAY,
            payload={"visible": True},
            state={"private": "kept in session"},
            is_terminal=True,
        )
    )
    monkeypatch.setattr("services.assistance.methods.top_n.TopNAssistance.start", start)
    response = client.post(
        "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
    )
    assert response.status_code == 200, response.text

    async def check():
        runner = client.app.state.preparation_runner
        await runner.close()
        async with runner.database.session() as db:
            row = (await db.execute(select(AssistancePreparation))).scalar_one()
            assert row.status == "complete"
            assert row.artifact_json is None
            assert row.spec_json == "{}"
            assert row.params_json == "{}"
            # Recent records are still available for request reconciliation.
            assert await runner.cleanup() == 0
            row.deadline_at = datetime.now(UTC) - timedelta(days=2)
            values = row.model_dump(exclude={"id", "identity"})
            db.add_all(
                [AssistancePreparation(**values, identity=f"expired-{i}") for i in range(500)]
            )
            await db.commit()
        assert await runner.cleanup() == 500
        assert await runner.cleanup() == 1
        async with runner.database.session() as db:
            assert (await db.execute(select(AssistanceSession))).scalar_one().id == response.json()[
                "session_id"
            ]

    client.portal.call(check)


def test_observation_is_owned_and_bounded(client, monkeypatch):
    _, headers, question = setup_rater(client)
    start = AsyncMock(return_value=InteractionStep(type=StepType.NONE, is_terminal=True))
    monkeypatch.setattr("services.assistance.methods.top_n.TopNAssistance.start", start)
    response = client.post(
        "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
    )
    assert response.status_code == 200
    payload = {"session_id": response.json()["session_id"], "wait_ms": 12.5}
    assert (
        client.post("/api/raters/assistance/observation", headers=headers, json=payload).status_code
        == 202
    )
    assert (
        client.post(
            "/api/raters/assistance/observation", headers=headers, json={**payload, "wait_ms": -1}
        ).status_code
        == 422
    )
    other = _rater_headers(_start_session(client, 1, "OTHER"))
    assert (
        client.post("/api/raters/assistance/observation", headers=other, json=payload).status_code
        == 404
    )


@pytest.mark.parametrize("original_prompt", [None, "Original study instructions"])
def test_advance_keeps_captured_system_prompt(client, monkeypatch, sync_engine, original_prompt):
    from sqlalchemy import text

    _, headers, question = setup_rater(client)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE experiments SET assistance_method='human_as_a_tool', system_prompt=:prompt WHERE id=1"
            ),
            {"prompt": original_prompt},
        )
    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.method.HumanAsAToolMethod.start",
        AsyncMock(return_value=InteractionStep(type=StepType.ASK_INPUT)),
    )
    advance = AsyncMock(return_value=InteractionStep(type=StepType.COMPLETE, is_terminal=True))
    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.method.HumanAsAToolMethod.advance", advance
    )
    started = client.post(
        "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
    )
    assert started.status_code == 200, started.text
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE experiments SET system_prompt='Edited after start' WHERE id=1"))
    response = client.post(
        "/api/raters/assistance/advance",
        headers=headers,
        json={"session_id": started.json()["session_id"], "human_input": "{}"},
    )
    assert response.status_code == 200, response.text
    assert advance.call_args.kwargs["experiment_system_prompt"] == original_prompt


def test_provider_events_link_preparation_and_human_turn(client, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from config import LLMSettings
    from services.assistance import llm

    session, headers, question = setup_rater(client)
    assert (
        client.patch(
            "/api/admin/experiments/1", json={"assistance_method": "human_as_a_tool"}
        ).status_code
        == 200
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(
                    return_value=SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                        usage=SimpleNamespace(total_tokens=12),
                    )
                )
            )
        )
    )
    monkeypatch.setattr(llm, "_get_client", lambda *args: fake_client)
    events = Mock()
    monkeypatch.setattr(llm.logger, "info", events)

    async def provider_step(*args, **kwargs):
        await llm.complete([], settings=LLMSettings(openrouter_api_key="test"))
        return InteractionStep(type=StepType.ASK_INPUT)

    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.method.HumanAsAToolMethod.start", provider_step
    )
    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.method.HumanAsAToolMethod.advance",
        provider_step,
    )
    started = client.post(
        "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
    )
    assert started.status_code == 200, started.text
    advanced = client.post(
        "/api/raters/assistance/advance",
        headers=headers,
        json={
            "session_id": started.json()["session_id"],
            "turn": 1,
            "human_input": "{}",
        },
    )
    assert advanced.status_code == 200, advanced.text
    recorded = [call.kwargs["extra"]["attributes"] for call in events.call_args_list]
    assert len(recorded) == 2
    assert all(
        event["rater_id"] == session["rater_id"]
        and event["question_id"] == question["id"]
        and event["method"] == "human_as_a_tool"
        for event in recorded
    )
    assert recorded[0]["preparation_id"] > 0
    assert recorded[1]["session_id"] == started.json()["session_id"]
    assert recorded[1]["experiment_id"] == 1


@pytest.mark.parametrize("failure", [None, "provider_error", "invalid_response", "execution_error"])
def test_failure_attribution_survives_cleanup_and_exports(client, monkeypatch, failure, caplog):
    import csv
    import io

    caplog.set_level("INFO", logger="services.assistance.runner")
    _, headers, question = setup_rater(client)
    if failure == "execution_error":
        start = AsyncMock(side_effect=RuntimeError("simulated execution failure"))
    else:
        start = AsyncMock(
            return_value=InteractionStep(
                type=StepType.NONE,
                is_terminal=True,
                failure_reason=failure,
            )
        )
    monkeypatch.setattr("services.assistance.methods.top_n.TopNAssistance.start", start)
    response = client.post(
        "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
    )
    assert response.status_code == 200, response.text
    assert "failure_reason" not in response.json()
    assert (
        client.post(
            "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
        ).json()
        == response.json()
    )
    assert start.await_count == 1
    assert (
        client.post(
            "/api/raters/submit",
            headers=headers,
            json={
                "question_id": question["id"],
                "answer": "Yes",
                "confidence": 4,
                "time_started": datetime.now(UTC).isoformat(),
            },
        ).status_code
        == 200
    )

    async def retire():
        runner = client.app.state.preparation_runner
        async with runner.database.session() as db:
            row = (await db.execute(select(AssistancePreparation))).scalar_one()
            row.deadline_at = datetime.now(UTC) - timedelta(days=2)
            await db.commit()
        assert await runner.cleanup() == 1

    client.portal.call(retire)
    exported = client.get("/api/admin/experiments/1/export")
    assert exported.status_code == 200
    row = list(csv.DictReader(io.StringIO(exported.text)))[0]
    assert row["assistance_method"] == "top_n"
    assert row["assistance_outcome"] == (failure or "no_assistance")
    completed = [
        record.attributes["assistance_outcome"]
        for record in caplog.records
        if getattr(record, "attributes", {}).get("prefetch.event") == "execution"
        and record.attributes.get("outcome") == "complete"
    ]
    assert completed == [failure or "no_assistance"]
