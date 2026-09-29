"""Activation favors unfinished work without continuously reshuffling the queue."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from models import AssistancePreparation
from services.assistance.base import InteractionStep, StepType

from test_question_queue import activate, enable, rating, reserve


def add_question(engine):
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO questions (experiment_id, question_id, question_text, question_type) VALUES (1, 'extra', 'Unfinished work', 'MC') RETURNING id"
            )
        ).scalar_one()


def add_coverage(engine, question_id, count=2, preview=False, reserved=False):
    with engine.begin() as conn:
        for i in range(count):
            rater = conn.execute(
                text(
                    "INSERT INTO raters (prolific_id, experiment_id, is_preview) VALUES (:pid, 1, :preview) RETURNING id"
                ),
                {"pid": f"coverage-{question_id}-{i}", "preview": preview},
            ).scalar_one()
            if reserved:
                conn.execute(
                    text(
                        "INSERT INTO question_assignments (rater_id, question_id, assigned_at, expires_at) VALUES (:rater, :question, now(), now() + interval '10 minutes')"
                    ),
                    {"rater": rater, "question": question_id},
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO ratings (rater_id, question_id, answer, confidence, time_started) VALUES (:rater, :question, 'Yes', 4, now())"
                    ),
                    {"rater": rater, "question": question_id},
                )


def pending_head(client, monkeypatch):
    session, headers, _ = enable(client, monkeypatch)
    state = reserve(client, headers)
    head, tail = state["items"]
    response = client.post("/api/raters/submit", headers=headers, json=rating(head))
    assert response.status_code == 200, response.text
    state = reserve(client, headers)
    assert state["items"] == [tail]
    return session, headers, state, tail


@pytest.mark.parametrize("queued", [False, True])
def test_completed_head_replaced_and_concurrent_retries_converge(
    client, monkeypatch, sync_engine, queued
):
    _, headers, state, head = pending_head(client, monkeypatch)
    alternative = add_question(sync_engine)
    if queued:
        state = reserve(client, headers)
        successor = state["items"][1]
    add_coverage(sync_engine, head["question"]["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: activate(client, headers, state, head), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    result = next(r.json() for r in responses if r.status_code == 200)
    replacement = result["items"][0]
    assert replacement["question"]["id"] == alternative
    assert replacement["activated"]
    assert len(result["items"]) == 1
    if queued:
        assert replacement == {**successor, "activated": True}
    assert activate(client, headers, state, head).status_code == 409
    assert activate(client, headers, result, replacement).json() == result
    with sync_engine.connect() as conn:
        assert conn.execute(
            text("SELECT completed_at IS NOT NULL FROM question_assignments WHERE id=:id"),
            {"id": head["assignment_id"]},
        ).scalar_one()


@pytest.mark.parametrize(
    "count,preview,reserved", [(1, False, False), (2, True, False), (2, False, True)]
)
def test_underfilled_head_is_kept_despite_lower_coverage_alternative(
    client, monkeypatch, sync_engine, count, preview, reserved
):
    _, headers, state, head = pending_head(client, monkeypatch)
    add_question(sync_engine)
    add_coverage(sync_engine, head["question"]["id"], count, preview, reserved)
    response = activate(client, headers, state, head)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0] == {**head, "activated": True}


def test_completed_head_kept_when_only_underfilled_question_already_rated(
    client, monkeypatch, sync_engine
):
    _, headers, state, head = pending_head(client, monkeypatch)
    add_coverage(sync_engine, head["question"]["id"])
    response = activate(client, headers, state, head)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0] == {**head, "activated": True}


@pytest.mark.parametrize("preview", [False, True])
def test_active_or_preview_question_never_replaced(client, monkeypatch, sync_engine, preview):
    session, headers, state, head = pending_head(client, monkeypatch)
    if preview:
        with sync_engine.begin() as conn:
            conn.execute(
                text("UPDATE raters SET is_preview=true WHERE id=:id"), {"id": session["rater_id"]}
            )
    else:
        state = activate(client, headers, state, head).json()
        head = state["items"][0]
    add_question(sync_engine)
    add_coverage(sync_engine, head["question"]["id"])
    response = activate(client, headers, state, head)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0] == {**head, "activated": True}


@pytest.mark.parametrize("ready", [False, True])
def test_replacement_cancels_prepared_or_inflight_work(client, monkeypatch, sync_engine, ready):
    _, headers, state, head = pending_head(client, monkeypatch)
    alternative = add_question(sync_engine)
    runner = client.app.state.preparation_runner
    client.portal.call(runner.close)
    response = client.post(
        "/api/raters/assistance/prepare",
        headers=headers,
        json={"assignment_id": head["assignment_id"], "generation": head["generation"]},
    )
    assert response.status_code == 202, response.text
    start = AsyncMock(
        return_value=InteractionStep(
            type=StepType.DISPLAY, payload={"candidates": []}, is_terminal=True
        )
    )
    monkeypatch.setattr("services.assistance.methods.top_n.TopNAssistance.start", start)

    async def claim():
        return await runner._claim(foreground_only=False)

    work = client.portal.call(claim)
    assert work is not None
    if ready:
        client.portal.call(runner._execute, work)
    add_coverage(sync_engine, head["question"]["id"])
    response = activate(client, headers, state, head)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["question"]["id"] == alternative
    if not ready:
        # A provider result arriving after release must not revive the old work.
        client.portal.call(runner._execute, work)

    async def check():
        async with client.app.state.database.session() as db:
            row = (await db.execute(select(AssistancePreparation))).scalar_one()
            assert row.status == "cancelled"
            assert row.owner_token is None

    client.portal.call(check)
    assert (
        client.post(
            "/api/raters/assistance/start",
            headers=headers,
            json={"question_id": head["question"]["id"]},
        ).status_code
        == 409
    )


def test_completed_parent_group_does_not_hide_unfinished_work(client, monkeypatch, sync_engine):
    _, headers, state, head = pending_head(client, monkeypatch)
    alternative = add_question(sync_engine)
    with sync_engine.begin() as conn:
        parent = conn.execute(
            text(
                "INSERT INTO questions (experiment_id, question_id, question_text, question_type) VALUES (1, 'context', 'Shared context', 'MC') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text("UPDATE questions SET parent_question_id=:parent WHERE id=:id"),
            {"parent": parent, "id": head["question"]["id"]},
        )
        sibling = conn.execute(
            text(
                "INSERT INTO questions (experiment_id, question_id, question_text, question_type, parent_question_id) VALUES (1, 'done-sibling', 'Completed sibling', 'MC', :parent) RETURNING id"
            ),
            {"parent": parent},
        ).scalar_one()
    add_coverage(sync_engine, head["question"]["id"])
    add_coverage(sync_engine, sibling)
    response = activate(client, headers, state, head)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["question"]["id"] == alternative
