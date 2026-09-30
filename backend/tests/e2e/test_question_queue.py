"""Queue protocol invariants against the real PostgreSQL database."""

import asyncio

import pytest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sqlalchemy import select, text

from config import get_settings
from models import AssistancePreparation
from services.assistance.base import InteractionStep, StepType
from test_preparation_runner import setup_rater


def enable(client, monkeypatch):
    session, headers, question = setup_rater(client)
    monkeypatch.setattr(get_settings().prefetch, "experiment_ids", [1])
    return session, headers, question


def reserve(client, headers):
    response = client.post("/api/raters/queue", headers=headers, json={})
    assert response.status_code == 200, response.text
    return response.json()


def activate(client, headers, state, item):
    return client.post(
        "/api/raters/queue",
        headers=headers,
        json={
            "action": "activate",
            "revision": state["revision"],
            "assignment_id": item["assignment_id"],
            "generation": item["generation"],
        },
    )


def rating(item):
    return {
        "question_id": item["question"]["id"],
        "answer": "Yes",
        "confidence": 4,
        "time_started": datetime.now(UTC).isoformat(),
        "assignment_id": item["assignment_id"],
        "assignment_generation": item["generation"],
    }


def test_concurrent_reserve_has_one_active_and_one_tail(client, monkeypatch):
    _, headers, _ = enable(client, monkeypatch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        states = list(pool.map(lambda _: reserve(client, headers), range(3)))
    assert states[0] == states[1] == states[2]
    assert len(states[0]["items"]) == 2
    assert [item["activated"] for item in states[0]["items"]] == [True, False]
    tail = states[0]["items"][1]
    assert activate(client, headers, states[0], tail).status_code == 409
    response = client.post("/api/raters/submit", headers=headers, json=rating(tail))
    assert response.status_code == 409
    response = client.post(
        "/api/raters/assistance/start",
        headers=headers,
        json={"question_id": tail["question"]["id"]},
    )
    assert response.status_code == 409
    assert (
        client.get("/api/raters/next-question", headers=headers).json()
        == states[0]["items"][0]["question"]
    )


def test_prepared_tail_is_consumed_once_after_activation_and_submit_is_idempotent(
    client, monkeypatch
):
    _, headers, _ = enable(client, monkeypatch)
    start = AsyncMock(
        return_value=InteractionStep(
            type=StepType.DISPLAY,
            payload={"candidates": []},
            state={"private": "secret"},
            is_terminal=True,
        )
    )
    monkeypatch.setattr("services.assistance.methods.top_n.TopNAssistance.start", start)
    state = reserve(client, headers)
    head, tail = state["items"]
    response = client.post(
        "/api/raters/assistance/prepare",
        headers=headers,
        json={
            "assignment_id": tail["assignment_id"],
            "generation": tail["generation"],
        },
    )
    assert response.status_code == 202

    async def ready():
        for _ in range(100):
            async with client.app.state.database.session() as db:
                row = (await db.execute(select(AssistancePreparation))).scalar_one_or_none()
                if row and row.status == "ready":
                    return
            await asyncio.sleep(0.02)
        raise AssertionError("Preparation did not finish")

    client.portal.call(ready)
    assert start.await_count == 1
    assert "secret" not in str(reserve(client, headers))
    payload = rating(head)
    first = client.post("/api/raters/submit", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    assert client.post("/api/raters/submit", headers=headers, json=payload).json() == first.json()
    assert (
        client.post(
            "/api/raters/submit", headers=headers, json={**payload, "answer": "different"}
        ).status_code
        == 409
    )
    state = reserve(client, headers)
    assert activate(client, headers, state, tail).status_code == 200
    first = client.post(
        "/api/raters/assistance/start",
        headers=headers,
        json={"question_id": tail["question"]["id"]},
    )
    assert first.status_code == 200, first.text
    assert (
        client.post(
            "/api/raters/assistance/start",
            headers=headers,
            json={"question_id": tail["question"]["id"]},
        ).json()
        == first.json()
    )
    assert start.await_count == 1
    assert "private" not in first.text


def test_grace_keeps_active_and_releases_tail(client, monkeypatch, backdate_rater_session):
    session, headers, _ = enable(client, monkeypatch)
    state = reserve(client, headers)
    head, tail = state["items"]
    backdate_rater_session(session["rater_id"], minutes_ago=61)
    state = reserve(client, headers)
    assert state["phase"] == "grace"
    assert state["items"] == [head]
    assert activate(client, headers, state, tail).status_code == 409
    assert client.post("/api/raters/submit", headers=headers, json=rating(head)).status_code == 200
    assert reserve(client, headers)["items"] == []


def test_end_releases_both_assignments(client, monkeypatch, sync_engine):
    session, headers, _ = enable(client, monkeypatch)
    reserve(client, headers)
    assert client.post("/api/raters/end-session", headers=headers).status_code == 200
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM question_assignments WHERE rater_id=:id AND completed_at IS NULL"
                ),
                {"id": session["rater_id"]},
            ).scalar_one()
            == 0
        )


def test_preparation_rejects_another_raters_assignment(client, monkeypatch):
    _, headers, _ = enable(client, monkeypatch)
    state = reserve(client, headers)
    from test_characterization import _start_session, _rater_headers

    other = _rater_headers(_start_session(client, 1, "OTHER"))
    item = state["items"][1]
    assert client.post(
        "/api/raters/assistance/prepare",
        headers=other,
        json={
            "assignment_id": item["assignment_id"],
            "generation": item["generation"],
        },
    ).status_code in (404, 409)


def test_preview_reset_rejects_old_token_and_clears_assignments(client, monkeypatch, sync_engine):
    _, _, _ = enable(client, monkeypatch)
    params = {
        "experiment_id": 1,
        "PROLIFIC_PID": "PREVIEW",
        "STUDY_ID": "preview",
        "SESSION_ID": "preview",
        "preview": True,
    }
    response = client.post("/api/raters/start", params=params)
    assert response.status_code == 200
    from test_characterization import _rater_headers

    old = _rater_headers(response.json())
    state = reserve(client, old)
    assert len(state["items"]) == 2
    new = client.post("/api/raters/start", params=params)
    assert new.status_code == 200
    assert client.post("/api/raters/queue", headers=old, json={}).status_code == 401
    state = reserve(client, _rater_headers(new.json()))
    assert len(state["items"]) == 2
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM question_assignments WHERE rater_id=:id"),
                {"id": new.json()["rater_id"]},
            ).scalar_one()
            == 2
        )


def test_kill_switch_preserves_active_submission_and_rejects_new_speculation(client, monkeypatch):
    _, headers, _ = enable(client, monkeypatch)
    state = reserve(client, headers)
    head, tail = state["items"]
    monkeypatch.setattr(get_settings().prefetch, "experiment_ids", [])
    assert not reserve(client, headers)["prefetch_enabled"]
    assert (
        client.post(
            "/api/raters/assistance/prepare",
            headers=headers,
            json={
                "assignment_id": tail["assignment_id"],
                "generation": tail["generation"],
            },
        ).status_code
        == 409
    )
    assert client.post("/api/raters/submit", headers=headers, json=rating(head)).status_code == 200
    state = reserve(client, headers)
    assert len(state["items"]) == 1
    assert activate(client, headers, state, state["items"][0]).status_code == 200


def test_stale_generation_cannot_activate_or_submit(client, monkeypatch):
    _, headers, _ = enable(client, monkeypatch)
    state = reserve(client, headers)
    head = state["items"][0]
    assert (
        activate(client, headers, state, {**head, "generation": head["generation"] + 1}).status_code
        == 409
    )
    payload = rating(head)
    payload["assignment_generation"] += 1
    assert client.post("/api/raters/submit", headers=headers, json=payload).status_code == 409


def test_reserved_successor_keeps_active_parent_context(client, monkeypatch, sync_engine):
    _, headers, question = enable(client, monkeypatch)
    with sync_engine.begin() as conn:
        parent = conn.execute(
            text(
                "INSERT INTO questions (experiment_id, question_id, question_text, question_type) VALUES (1, 'parent', 'Shared context', 'MC') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text("UPDATE questions SET parent_question_id=:parent WHERE id=:id"),
            {"parent": parent, "id": question["id"]},
        )
        sibling = conn.execute(
            text(
                "INSERT INTO questions (experiment_id, question_id, question_text, question_type, parent_question_id) VALUES (1, 'sibling', 'Related question', 'MC', :parent) RETURNING id"
            ),
            {"parent": parent},
        ).scalar_one()
    state = reserve(client, headers)
    assert state["items"][1]["question"]["id"] == sibling
    assert state["items"][1]["question"]["parent_question_text"] == "Shared context"


def test_expired_successor_can_be_reserved_again_with_a_new_generation(
    client, monkeypatch, sync_engine
):
    _, headers, _ = enable(client, monkeypatch)
    state = reserve(client, headers)
    head, tail = state["items"]
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE question_assignments SET expires_at = now() - interval '1 second' WHERE id=:id"
            ),
            {"id": tail["assignment_id"]},
        )
    refreshed = reserve(client, headers)
    assert refreshed["items"][0] == head
    assert len(refreshed["items"]) == 2
    replacement = refreshed["items"][1]
    assert replacement["assignment_id"] == tail["assignment_id"]
    assert replacement["generation"] == tail["generation"] + 1
    assert (
        client.post(
            "/api/raters/assistance/prepare",
            headers=headers,
            json={
                "assignment_id": tail["assignment_id"],
                "generation": tail["generation"],
            },
        ).status_code
        == 409
    )


@pytest.mark.parametrize("before_read", [False, True])
@pytest.mark.parametrize("path,body", [("queue", {}), ("end-session", None)])
def test_reset_while_authenticated_request_waits_for_lock(
    client, monkeypatch, sync_engine, path, body, before_read
):
    session, headers, _ = setup_rater(client)
    from services.rater import queue

    original = queue._acquire_assignment_lock

    async def reset_before_lock(experiment_id, db):
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE raters SET session_start = session_start + interval '1 second' WHERE id=:id"
                ),
                {"id": session["rater_id"]},
            )
        await original(experiment_id, db)

    if before_read:
        original_lock = queue.lock_rater

        async def reset_before_read(rater_id, db):
            await reset_before_lock(1, db)
            db.expire_all()
            return await original_lock(rater_id, db)

        monkeypatch.setattr(queue, "lock_rater", reset_before_read)
    else:
        monkeypatch.setattr(queue, "_acquire_assignment_lock", reset_before_lock)
    response = client.post(f"/api/raters/{path}", headers=headers, json=body)
    assert response.status_code == 401, response.text
    with sync_engine.connect() as conn:
        assert conn.execute(
            text("SELECT is_active FROM raters WHERE id=:id"), {"id": session["rater_id"]}
        ).scalar_one()


@pytest.mark.parametrize("lookahead", [0, 1, 3, 5])
def test_configurable_lookahead_is_bounded_and_refills(client, monkeypatch, lookahead):
    _, headers, _ = enable(client, monkeypatch)
    csv_data = "question_id,question_text,gt_answer,options,question_type\n" + "\n".join(
        f"extra-{i},Question {i}?,Yes,Yes|No,MC" for i in range(12)
    )
    assert (
        client.post(
            "/api/admin/experiments/1/upload", files={"file": ("more.csv", csv_data, "text/csv")}
        ).status_code
        == 200
    )
    monkeypatch.setattr(get_settings().prefetch, "lookahead_questions", lookahead)
    state = reserve(client, headers)
    assert len(state["items"]) == lookahead + 1
    assert sum(item["activated"] for item in state["items"]) == 1
    head = state["items"][0]
    assert client.post("/api/raters/submit", headers=headers, json=rating(head)).status_code == 200
    refilled = reserve(client, headers)
    assert len(refilled["items"]) == lookahead + 1
    assert len({item["assignment_id"] for item in refilled["items"]}) == lookahead + 1
    assert all(item["question"]["id"] != head["question"]["id"] for item in refilled["items"])


def test_queue_rollout_is_opt_in_and_existing_sessions_drain(client, monkeypatch):
    from test_characterization import _start_session

    session, headers, _ = setup_rater(client)
    assert not session["queue_enabled"]
    monkeypatch.setattr(get_settings().prefetch, "experiment_ids", [1])
    assert _start_session(client, 1)["queue_enabled"]
    reserve(client, headers)
    delayed = _start_session(client, 1, "DELAYED_ON_INTRO")
    assert delayed["queue_enabled"]
    monkeypatch.setattr(get_settings().prefetch, "experiment_ids", [])
    assert _start_session(client, 1)["queue_enabled"]
    assert not _start_session(client, 1, "NEW_PARTICIPANT")["queue_enabled"]
    assert not reserve(client, headers)["prefetch_enabled"]
    from test_characterization import _rater_headers

    # The already-issued client must not get stuck retrying its first queue
    # request after rollout removal, even though it had not entered queue mode.
    state = reserve(client, _rater_headers(delayed))
    assert len(state["items"]) == 1
    assert not state["prefetch_enabled"]
