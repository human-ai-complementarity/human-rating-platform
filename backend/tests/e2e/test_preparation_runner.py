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
