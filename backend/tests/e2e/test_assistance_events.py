"""The append-only assistance event log.

Drives real sessions through /api/raters/assistance/* with stub methods
registered for the test, then reads ``assistance_events`` back to check that
every method call left a request row and a response row with the right
step type, status, latency and snapshot — including the failure paths that
the session row alone cannot explain after the fact.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from services.assistance.base import AssistanceMethod, InteractionStep, StepType
from services.assistance.registry import register

# ---------------------------------------------------------------------------
# Stub methods
# ---------------------------------------------------------------------------


class _TwoTurn(AssistanceMethod):
    """ASK_INPUT on start, COMPLETE on the first advance."""

    async def start(
        self, question, params, *, parent_question_text=None, experiment_system_prompt=None
    ):
        return InteractionStep(
            type=StepType.ASK_INPUT,
            payload={"prompt": "first?"},
            state={"turn": 1, "secret": "backend-only"},
        )

    async def advance(self, state, human_input, params, *, experiment_system_prompt=None):
        return InteractionStep(
            type=StepType.COMPLETE,
            payload={"answer": human_input.upper(), "turns": state["turn"] + 1},
            is_terminal=True,
        )


class _RaisesOnStart(AssistanceMethod):
    calls = 0

    async def start(
        self, question, params, *, parent_question_text=None, experiment_system_prompt=None
    ):
        type(self).calls += 1
        raise RuntimeError("provider exploded")


class _DegradesOnStart(AssistanceMethod):
    """Catches its own failure and returns a NONE step with a failure_reason."""

    async def start(
        self, question, params, *, parent_question_text=None, experiment_system_prompt=None
    ):
        return InteractionStep(
            type=StepType.NONE, is_terminal=True, failure_reason="provider_error"
        )


class _RaisesOnAdvance(_TwoTurn):
    async def advance(self, state, human_input, params, *, experiment_system_prompt=None):
        raise RuntimeError("mid-session failure")


class _TimesOutOnAdvance(_TwoTurn):
    async def advance(self, state, human_input, params, *, experiment_system_prompt=None):
        raise TimeoutError("deadline exceeded")


class _FailsThenSlow(AssistanceMethod):
    """First start raises; later starts take long enough to overlap a retry."""

    calls = 0

    async def start(
        self, question, params, *, parent_question_text=None, experiment_system_prompt=None
    ):
        type(self).calls += 1
        if type(self).calls == 1:
            raise RuntimeError("first attempt fails")
        await asyncio.sleep(0.5)
        return InteractionStep(
            type=StepType.ASK_INPUT, payload={"attempt": type(self).calls}, state={"x": 1}
        )


class _SlowAdvance(_TwoTurn):
    """Non-terminal advance that takes long enough for a duplicate submit to overlap."""

    advances = 0

    async def advance(self, state, human_input, params, *, experiment_system_prompt=None):
        type(self).advances += 1
        await asyncio.sleep(0.5)
        return InteractionStep(
            type=StepType.ASK_INPUT,
            payload={"prompt": f"round {state['turn'] + 1}", "got": human_input},
            state={"turn": state["turn"] + 1},
        )


_METHODS = {
    "test_fails_then_slow": _FailsThenSlow,
    "test_slow_advance": _SlowAdvance,
    "test_two_turn": _TwoTurn,
    "test_raises_on_start": _RaisesOnStart,
    "test_degrades_on_start": _DegradesOnStart,
    "test_raises_on_advance": _RaisesOnAdvance,
    "test_times_out_on_advance": _TimesOutOnAdvance,
}


@pytest.fixture(autouse=True)
def _register_methods():
    for name, cls in _METHODS.items():
        register(name, cls)
    _RaisesOnStart.calls = 0
    _FailsThenSlow.calls = 0
    _SlowAdvance.advances = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup(client: TestClient, method: str) -> tuple[dict, int]:
    """Create an experiment on `method`, upload a question, start a rater session.

    Returns (rater headers, question id).
    """
    response = client.post(
        "/api/admin/experiments",
        json={
            "name": f"events-{uuid4().hex[:8]}",
            "num_ratings_per_question": 1,
            "assistance_method": method,
        },
    )
    assert response.status_code == 200, response.text
    experiment_id = response.json()["id"]

    response = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={
            "file": (
                "questions.csv",
                "question_id,question_text,gt_answer,options,question_type\n"
                "q1,Is this useful?,Yes,Yes|No,MC\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 200, response.text

    response = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment_id,
            "PROLIFIC_PID": "PID_EVENTS",
            "STUDY_ID": "STUDY_1",
            "SESSION_ID": "SESSION_EVENTS",
        },
    )
    assert response.status_code == 200, response.text
    headers = {"X-Rater-Session": response.json()["rater_session_token"]}

    question = client.get("/api/raters/next-question", headers=headers).json()
    return headers, question["id"]


def _events(sync_engine, session_id: int) -> list[dict]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT direction, step_type, status, latency_ms, payload, error
                  FROM assistance_events
                 WHERE assistance_session_id = :sid
                 ORDER BY id
                """
            ),
            {"sid": session_id},
        ).mappings()
        return [
            {**dict(row), "payload": json.loads(row["payload"]) if row["payload"] else None}
            for row in rows
        ]


def _session_rows(sync_engine, question_id: int) -> list[dict]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, step_type, is_complete, method_name FROM assistance_sessions "
                "WHERE question_id = :qid ORDER BY id"
            ),
            {"qid": question_id},
        ).mappings()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_two_turn_session_logs_every_step(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_two_turn")

    started = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    )
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]
    assert started.json()["turn"] == 1

    advanced = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": session_id, "human_input": "yes", "turn": 1},
        headers=headers,
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["type"] == "complete"
    assert advanced.json()["turn"] == 2

    events = _events(sync_engine, session_id)
    assert [(e["direction"], e["step_type"], e["status"]) for e in events] == [
        ("request", None, "ok"),
        ("response", "ask_input", "ok"),
        ("request", "ask_input", "ok"),
        ("response", "complete", "ok"),
    ]

    start_request, start_response, advance_request, advance_response = events
    assert start_request["payload"] == {"params": {}}
    assert start_request["latency_ms"] is None

    # The intermediate step — overwritten on the session row by now — is intact,
    # rater-facing payload and backend state both.
    assert start_response["payload"] == {
        "payload": {"prompt": "first?"},
        "state": {"turn": 1, "secret": "backend-only"},
        "is_terminal": False,
    }
    assert start_response["latency_ms"] is not None and start_response["latency_ms"] >= 0

    assert advance_request["payload"] == {"human_input": "yes"}
    assert advance_response["payload"] == {
        "payload": {"answer": "YES", "turns": 2},
        "state": {},
        "is_terminal": True,
    }
    assert all(e["error"] is None for e in events)

    # The session row itself only knows the final step.
    (session,) = _session_rows(sync_engine, question_id)
    assert session["step_type"] == "complete" and session["is_complete"] is True


def test_resuming_an_open_session_adds_no_events(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_two_turn")
    first = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()
    second = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()

    assert second == first
    assert len(_events(sync_engine, first["session_id"])) == 2


def test_start_raising_is_logged_as_error_with_none_fallback(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_raises_on_start")

    started = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    )
    assert started.status_code == 200, started.text
    assert started.json()["type"] == "none"

    events = _events(sync_engine, started.json()["session_id"])
    assert [(e["direction"], e["status"]) for e in events] == [
        ("request", "ok"),
        ("response", "error"),
    ]
    response = events[1]
    assert response["step_type"] == "none"
    assert response["error"] == "RuntimeError: provider exploded"
    assert response["payload"] == {"payload": {}, "state": {}, "is_terminal": True}


def test_method_reported_failure_is_logged_as_error(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_degrades_on_start")

    started = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    )
    assert started.json()["type"] == "none"

    response = _events(sync_engine, started.json()["session_id"])[1]
    assert response["status"] == "error"
    assert response["error"] == "provider_error"
    assert response["payload"]["failure_reason"] == "provider_error"


def test_retrying_a_failed_session_keeps_its_history(client: TestClient, sync_engine):
    """A NONE session is retried on the next start; the earlier attempt's events survive."""
    headers, question_id = _setup(client, "test_raises_on_start")

    first = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()
    second = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()

    assert _RaisesOnStart.calls == 2
    assert second["session_id"] == first["session_id"]
    assert len(_session_rows(sync_engine, question_id)) == 1

    events = _events(sync_engine, first["session_id"])
    assert [(e["direction"], e["status"]) for e in events] == [
        ("request", "ok"),
        ("response", "error"),
        ("request", "ok"),
        ("response", "error"),
    ]


def test_concurrent_retries_of_a_failed_session_serialize(client: TestClient, sync_engine):
    """Two overlapping retries of a NONE session: one runs the method, the other waits
    on the row lock and returns the same step. One event pair, not two."""
    headers, question_id = _setup(client, "test_fails_then_slow")
    first = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()
    assert first["type"] == "none"

    def _retry() -> dict:
        response = client.post(
            "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: _retry(), range(2)))

    assert a == b
    assert a["session_id"] == first["session_id"]
    assert a["type"] == "ask_input"
    assert _FailsThenSlow.calls == 2  # the failed attempt plus exactly one retry

    events = _events(sync_engine, first["session_id"])
    assert [(e["direction"], e["step_type"], e["status"]) for e in events] == [
        ("request", None, "ok"),
        ("response", "none", "error"),
        ("request", None, "ok"),
        ("response", "ask_input", "ok"),
    ]
    (session,) = _session_rows(sync_engine, question_id)
    assert session["step_type"] == "ask_input"


def test_advance_raising_is_logged_as_error_with_skip_fallback(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_raises_on_advance")
    session_id = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()["session_id"]

    advanced = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": session_id, "human_input": "x"},
        headers=headers,
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["type"] == "skip"

    events = _events(sync_engine, session_id)
    assert len(events) == 4
    assert events[2]["payload"] == {"human_input": "x"}
    assert events[2]["step_type"] == "ask_input"
    assert (events[3]["step_type"], events[3]["status"]) == ("skip", "error")
    assert events[3]["error"] == "RuntimeError: mid-session failure"


def test_advance_timing_out_is_logged_as_timeout(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_times_out_on_advance")
    session_id = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()["session_id"]

    advanced = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": session_id, "human_input": "x"},
        headers=headers,
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["type"] == "skip"

    last = _events(sync_engine, session_id)[-1]
    assert (last["direction"], last["step_type"], last["status"]) == ("response", "skip", "timeout")
    assert last["error"] == "TimeoutError: deadline exceeded"


def test_events_go_with_their_session(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_two_turn")
    session_id = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()["session_id"]
    assert _events(sync_engine, session_id)

    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM assistance_sessions WHERE id = :sid"), {"sid": session_id})
    assert _events(sync_engine, session_id) == []


def test_concurrent_duplicate_advances_apply_once(client: TestClient, sync_engine):
    """Two overlapping advances answering the same turn: the method runs once, both
    callers get the same next step, and the log has one request/response pair."""
    headers, question_id = _setup(client, "test_slow_advance")
    started = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()

    def _advance() -> dict:
        response = client.post(
            "/api/raters/assistance/advance",
            json={
                "session_id": started["session_id"],
                "human_input": "yes",
                "turn": started["turn"],
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: _advance(), range(2)))

    assert a == b
    assert a["type"] == "ask_input"
    assert a["payload"] == {"prompt": "round 2", "got": "yes"}
    assert a["turn"] == 2
    assert _SlowAdvance.advances == 1

    events = _events(sync_engine, started["session_id"])
    assert [(e["direction"], e["step_type"]) for e in events] == [
        ("request", None),
        ("response", "ask_input"),
        ("request", "ask_input"),
        ("response", "ask_input"),
    ]


def test_stale_turn_returns_current_step_without_advancing(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_slow_advance")
    started = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()
    first = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": started["session_id"], "human_input": "one", "turn": 1},
        headers=headers,
    ).json()
    assert first["turn"] == 2

    # Re-sends the answer to turn 1, which has already been consumed.
    replay = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": started["session_id"], "human_input": "one", "turn": 1},
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == first
    assert _SlowAdvance.advances == 1
    assert len(_events(sync_engine, started["session_id"])) == 4

    # Answering the current turn advances as normal.
    second = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": started["session_id"], "human_input": "two", "turn": 2},
        headers=headers,
    ).json()
    assert second["turn"] == 3 and second["payload"]["got"] == "two"
    assert _SlowAdvance.advances == 2


def test_advance_without_turn_is_not_deduplicated(client: TestClient, sync_engine):
    """Older clients that send no turn keep the pre-existing behaviour."""
    headers, question_id = _setup(client, "test_slow_advance")
    session_id = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()["session_id"]
    for human_input in ("one", "one"):
        response = client.post(
            "/api/raters/assistance/advance",
            json={"session_id": session_id, "human_input": human_input},
            headers=headers,
        )
        assert response.status_code == 200, response.text
    assert _SlowAdvance.advances == 2
    assert len(_events(sync_engine, session_id)) == 6


def test_stale_turn_on_a_completed_session_returns_the_final_step(client: TestClient):
    headers, question_id = _setup(client, "test_two_turn")
    started = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()
    final = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": started["session_id"], "human_input": "yes", "turn": 1},
        headers=headers,
    ).json()
    assert final["type"] == "complete"

    # A duplicate of the completing submit gets the completed step, not a 400.
    replay = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": started["session_id"], "human_input": "yes", "turn": 1},
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == final

    # Whereas a genuinely new input against a finished session is still refused.
    again = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": started["session_id"], "human_input": "more", "turn": 2},
        headers=headers,
    )
    assert again.status_code == 400


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


def _experiment_id_for(sync_engine, question_id: int) -> int:
    with sync_engine.connect() as conn:
        return conn.execute(
            text("SELECT experiment_id FROM questions WHERE id = :qid"), {"qid": question_id}
        ).scalar_one()


def test_admin_session_detail_returns_decoded_event_log(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_two_turn")
    session_id = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()["session_id"]
    client.post(
        "/api/raters/assistance/advance",
        json={"session_id": session_id, "human_input": "yes"},
        headers=headers,
    )

    response = client.get(f"/api/admin/assistance-sessions/{session_id}")
    assert response.status_code == 200, response.text
    detail = response.json()

    assert detail["id"] == session_id
    assert detail["question_id"] == question_id
    assert detail["method_name"] == "test_two_turn"
    assert detail["params"] == {}
    assert detail["step_type"] == "complete"
    assert detail["is_complete"] is True
    assert detail["payload"] == {"answer": "YES", "turns": 2}
    assert detail["event_count"] == 4

    events = detail["events"]
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    assert [(e["direction"], e["step_type"], e["status"]) for e in events] == [
        ("request", None, "ok"),
        ("response", "ask_input", "ok"),
        ("request", "ask_input", "ok"),
        ("response", "complete", "ok"),
    ]
    # JSON columns come back decoded, not as strings.
    assert events[1]["payload"]["payload"] == {"prompt": "first?"}
    assert events[2]["payload"] == {"human_input": "yes"}
    assert events[1]["latency_ms"] is not None
    assert all("created_at" in e for e in events)


def test_admin_session_detail_404_for_unknown_session(client: TestClient):
    response = client.get("/api/admin/assistance-sessions/999999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Assistance session not found"


def test_admin_session_list_filters_and_counts(client: TestClient, sync_engine):
    headers, question_id = _setup(client, "test_raises_on_advance")
    experiment_id = _experiment_id_for(sync_engine, question_id)

    session_id = client.post(
        "/api/raters/assistance/start", json={"question_id": question_id}, headers=headers
    ).json()["session_id"]

    listed = client.get(f"/api/admin/experiments/{experiment_id}/assistance-sessions")
    assert listed.status_code == 200, listed.text
    (row,) = listed.json()
    assert row["id"] == session_id
    assert row["step_type"] == "ask_input"
    assert row["event_count"] == 2
    assert "events" not in row

    # Fail the advance so the session lands on `skip`, then find it by step type.
    client.post(
        "/api/raters/assistance/advance",
        json={"session_id": session_id, "human_input": "x"},
        headers=headers,
    )
    skipped = client.get(
        f"/api/admin/experiments/{experiment_id}/assistance-sessions",
        params={"step_type": "skip"},
    ).json()
    assert [s["id"] for s in skipped] == [session_id]
    assert skipped[0]["event_count"] == 4

    assert (
        client.get(
            f"/api/admin/experiments/{experiment_id}/assistance-sessions",
            params={"step_type": "complete"},
        ).json()
        == []
    )
    assert (
        client.get(
            f"/api/admin/experiments/{experiment_id}/assistance-sessions",
            params={"question_id": question_id, "rater_id": row["rater_id"]},
        ).json()
        != []
    )
    assert (
        client.get(
            f"/api/admin/experiments/{experiment_id}/assistance-sessions",
            params={"rater_id": row["rater_id"] + 1},
        ).json()
        == []
    )


def test_admin_session_list_is_scoped_to_the_experiment(client: TestClient, sync_engine):
    headers_a, question_a = _setup(client, "test_two_turn")
    client.post("/api/raters/assistance/start", json={"question_id": question_a}, headers=headers_a)
    headers_b, question_b = _setup(client, "test_two_turn")
    client.post("/api/raters/assistance/start", json={"question_id": question_b}, headers=headers_b)

    experiment_b = _experiment_id_for(sync_engine, question_b)
    listed = client.get(f"/api/admin/experiments/{experiment_b}/assistance-sessions").json()
    assert [s["question_id"] for s in listed] == [question_b]

    assert client.get("/api/admin/experiments/999999/assistance-sessions").status_code == 404
