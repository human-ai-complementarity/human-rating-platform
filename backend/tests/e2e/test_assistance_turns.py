"""Turn retries reconcile one step without racing provider work or stale owners."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from services.assistance.base import InteractionStep, StepType
from test_preparation_runner import setup_rater


def setup_turn(client, monkeypatch):
    _, headers, question = setup_rater(client)
    assert (
        client.patch(
            "/api/admin/experiments/1", json={"assistance_method": "human_as_a_tool"}
        ).status_code
        == 200
    )
    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.method.HumanAsAToolMethod.start",
        AsyncMock(return_value=InteractionStep(type=StepType.ASK_INPUT)),
    )
    response = client.post(
        "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
    )
    assert response.status_code == 200, response.text
    return (
        headers,
        question,
        {
            "session_id": response.json()["session_id"],
            "human_input": "{}",
            "expected_revision": response.json()["revision"],
        },
    )


def test_retry_during_and_after_advance_runs_one_turn(client, monkeypatch):
    headers, question, body = setup_turn(client, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return InteractionStep(type=StepType.ASK_INPUT, payload={"iteration": 2})

    advance = AsyncMock(side_effect=delayed)
    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.method.HumanAsAToolMethod.advance", advance
    )
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(
            client.post, "/api/raters/assistance/advance", headers=headers, json=body
        )
        try:
            assert entered.wait(5)
            assert (
                client.post(
                    "/api/raters/assistance/advance", headers=headers, json=body
                ).status_code
                == 409
            )
            old = client.post(
                "/api/raters/assistance/start",
                headers=headers,
                json={"question_id": question["id"]},
            )
            assert old.json()["revision"] == 0
        finally:
            release.set()
        first = pending.result(5)
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1
    assert (
        client.post("/api/raters/assistance/advance", headers=headers, json=body).json()
        == first.json()
    )
    assert advance.await_count == 1
    restored = client.post(
        "/api/raters/assistance/start", headers=headers, json={"question_id": question["id"]}
    )
    assert restored.json() == first.json()


def test_expired_turn_owner_cannot_overwrite_recovered_result(client, monkeypatch, sync_engine):
    headers, _, body = setup_turn(client, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = 0

    async def delayed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return InteractionStep(type=StepType.ASK_INPUT, payload={"owner": "old"})
        return InteractionStep(type=StepType.COMPLETE, payload={"owner": "new"}, is_terminal=True)

    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.method.HumanAsAToolMethod.advance", delayed
    )
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(
            client.post, "/api/raters/assistance/advance", headers=headers, json=body
        )
        try:
            assert entered.wait(5)
            with sync_engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE assistance_sessions SET advance_expires_at=now()-interval '1 second' WHERE id=:id"
                    ),
                    {"id": body["session_id"]},
                )
            second = client.post("/api/raters/assistance/advance", headers=headers, json=body)
            assert second.status_code == 200, second.text
        finally:
            release.set()
        assert pending.result(5).status_code == 409
    assert (
        client.post("/api/raters/assistance/advance", headers=headers, json=body).json()
        == second.json()
    )
    assert second.json()["payload"] == {"owner": "new"}


def test_end_during_advance_prevents_publication(client, monkeypatch, sync_engine):
    headers, _, body = setup_turn(client, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return InteractionStep(type=StepType.COMPLETE, is_terminal=True)

    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.method.HumanAsAToolMethod.advance", delayed
    )
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(
            client.post, "/api/raters/assistance/advance", headers=headers, json=body
        )
        try:
            assert entered.wait(5)
            assert client.post("/api/raters/end-session", headers=headers).status_code == 200
        finally:
            release.set()
        assert pending.result(5).status_code == 403
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT revision FROM assistance_sessions WHERE id=:id"),
                {"id": body["session_id"]},
            ).scalar_one()
            == 0
        )


def test_unexpected_failure_releases_turn_for_retry(client, monkeypatch):
    headers, _, body = setup_turn(client, monkeypatch)
    advance = AsyncMock(
        side_effect=[
            ValueError("bad input"),
            InteractionStep(type=StepType.COMPLETE, is_terminal=True),
        ]
    )
    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.method.HumanAsAToolMethod.advance", advance
    )
    with pytest.raises(ValueError, match="bad input"):
        client.post("/api/raters/assistance/advance", headers=headers, json=body)
    assert (
        client.post("/api/raters/assistance/advance", headers=headers, json=body).status_code == 200
    )
