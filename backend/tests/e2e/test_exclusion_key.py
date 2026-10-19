"""A rater is served at most one of the questions sharing an exclusion_key (#157)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from config import get_settings
from services.assistance.base import InteractionStep, StepType
from services.rater.queries import fetch_eligible_questions_with_counts, fetch_rated_question_ids
from test_characterization import (
    _build_parquet_bytes,
    _create_experiment,
    _get_next_question,
    _start_session,
    _submit,
    _upload_questions,
)

# a and b are two versions of one item; c has no key; d has a key of its own.
KEYED_CSV = (
    "question_id,question_text,exclusion_key\n"
    "a,Vulnerable version?,commit-1\n"
    "b,Patched version?,commit-1\n"
    "c,Unrelated?,\n"
    "d,Other commit?,commit-2\n"
)


def _upload(client: TestClient, experiment_id: int, csv_data: str) -> None:
    response = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("keyed.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 200, response.text


def _keys(sync_engine, experiment_id: int) -> dict[str, str | None]:
    with sync_engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT question_id, exclusion_key FROM questions WHERE experiment_id = :id"),
                {"id": experiment_id},
            ).all()
        )


def _row_ids(sync_engine, experiment_id: int) -> dict[str, int]:
    with sync_engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT question_id, id FROM questions WHERE experiment_id = :id"),
                {"id": experiment_id},
            ).all()
        )


def _seed_rating(sync_engine, rater_id: int, question_id: int) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ratings (rater_id, question_id, answer, confidence, time_started) "
                "VALUES (:rater, :question, 'Yes', 4, now())"
            ),
            {"rater": rater_id, "question": question_id},
        )


RESERVATION_STATES = ("skipped", "expired", "queued")


def _seed_reservation(sync_engine, rater_id: int, question_id: int, state: str) -> None:
    now = datetime.now(UTC)
    # A skip releases the reservation, an abandoned question lets it expire,
    # and a queued successor holds it live without ever being shown.
    assigned_at, expires_at, completed_at, activated_at = {
        "skipped": (now - timedelta(minutes=1), now, now, now - timedelta(minutes=1)),
        "expired": (
            now - timedelta(minutes=40),
            now - timedelta(minutes=10),
            None,
            now - timedelta(minutes=40),
        ),
        "queued": (now, now + timedelta(minutes=10), None, None),
    }[state]
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO question_assignments "
                "(rater_id, question_id, assigned_at, expires_at, completed_at, activated_at) "
                "VALUES (:rater, :question, :assigned, :expires, :completed, :activated)"
            ),
            {
                "rater": rater_id,
                "question": question_id,
                "assigned": assigned_at,
                "expires": expires_at,
                "completed": completed_at,
                "activated": activated_at,
            },
        )


def _eligible(client: TestClient, experiment_id: int, rater_id: int) -> set[str]:
    async def fetch():
        async with client.app.state.database.session() as db:
            rows = await fetch_eligible_questions_with_counts(
                experiment_id=experiment_id,
                rated_question_ids=await fetch_rated_question_ids(rater_id, db),
                rater_id=rater_id,
                now=datetime.now(UTC),
                db=db,
            )
            return {question.question_id for question, _, _ in rows}

    return client.portal.call(fetch)


def test_csv_upload_reads_exclusion_key_and_stores_blank_as_null(client, sync_engine):
    experiment = _create_experiment(client)
    _upload(
        client,
        experiment["id"],
        "question_id,question_text,exclusion_key\n"
        "a,Vulnerable version?,commit-1\n"
        "b,Patched version?, commit-1 \n"
        "c,Unrelated?,\n"
        "d,Whitespace only?,   \n",
    )
    assert _keys(sync_engine, experiment["id"]) == {
        "a": "commit-1",
        "b": "commit-1",
        "c": None,
        "d": None,
    }


def test_upload_without_exclusion_key_column_leaves_questions_unkeyed(client, sync_engine):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    assert _keys(sync_engine, experiment["id"]) == {"q1": None, "q2": None}


def test_parquet_upload_reads_exclusion_key_and_stores_blank_as_null(client, sync_engine):
    experiment = _create_experiment(client)
    rows = [
        {"question_id": "a", "question_text": "Vulnerable version?", "exclusion_key": "commit-1"},
        {"question_id": "b", "question_text": "Patched version?", "exclusion_key": " commit-1 "},
        {"question_id": "c", "question_text": "Unrelated?", "exclusion_key": None},
        {"question_id": "d", "question_text": "Whitespace only?", "exclusion_key": "   "},
    ]
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={
            "file": ("keyed.parquet", _build_parquet_bytes(rows=rows), "application/octet-stream")
        },
    )
    assert response.status_code == 200, response.text
    assert _keys(sync_engine, experiment["id"]) == {
        "a": "commit-1",
        "b": "commit-1",
        "c": None,
        "d": None,
    }


def test_upload_rejects_an_exclusion_key_longer_than_its_column(client, sync_engine):
    # a sits exactly at the limit, so naming b shows where the boundary is.
    experiment = _create_experiment(client)
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={
            "file": (
                "keyed.csv",
                "question_id,question_text,exclusion_key\n"
                f"a,At the limit?,{'k' * 255}\n"
                f"b,Over the limit?,{'k' * 256}\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 400
    assert "'b'" in response.json()["detail"]
    assert _keys(sync_engine, experiment["id"]) == {}


def test_duplicate_copies_exclusion_key(client, sync_engine):
    experiment = _create_experiment(client)
    _upload(client, experiment["id"], KEYED_CSV)
    copy = client.post(f"/api/admin/experiments/{experiment['id']}/duplicate").json()
    assert _keys(sync_engine, copy["id"]) == {
        "a": "commit-1",
        "b": "commit-1",
        "c": None,
        "d": "commit-2",
    }


def test_export_includes_exclusion_key(client, sync_engine):
    experiment = _create_experiment(client)
    _upload(client, experiment["id"], KEYED_CSV)
    ids = _row_ids(sync_engine, experiment["id"])
    session = _start_session(client, experiment["id"])
    for name in ("a", "c"):
        _seed_rating(sync_engine, session["rater_id"], ids[name])

    with client.stream("GET", f"/api/admin/experiments/{experiment['id']}/export") as response:
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO("".join(response.iter_text()))))

    assert {row["question_id"]: row["exclusion_key"] for row in rows} == {"a": "commit-1", "c": ""}


def test_recommendation_counts_a_key_as_one_unit(client, sync_engine):
    # Target 2 and e already rated once: 9 ratings still needed. A rater takes
    # at most one question per pair, so each pair needs 4 distinct raters.
    # Counting questions instead would recommend 2 places.
    experiment = _create_experiment(client)
    _upload(
        client,
        experiment["id"],
        "question_id,question_text,exclusion_key\n"
        "a,Vulnerable version?,commit-1\n"
        "b,Patched version?,commit-1\n"
        "c,Vulnerable version?,commit-2\n"
        "d,Patched version?,commit-2\n"
        "e,Unrelated?,\n",
    )
    ids = _row_ids(sync_engine, experiment["id"])
    session = _start_session(client, experiment["id"])
    _seed_rating(sync_engine, session["rater_id"], ids["e"])

    response = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/recommend")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["remaining_rating_actions"] == 9
    assert payload["recommended_places"] == 4


def test_rating_one_question_drops_its_partner(client, sync_engine):
    experiment = _create_experiment(client)
    _upload(client, experiment["id"], KEYED_CSV)
    ids = _row_ids(sync_engine, experiment["id"])
    session = _start_session(client, experiment["id"])
    _seed_rating(sync_engine, session["rater_id"], ids["a"])

    assert _eligible(client, experiment["id"], session["rater_id"]) == {"c", "d"}


@pytest.mark.parametrize("state", RESERVATION_STATES)
def test_any_reservation_drops_the_partner_but_not_the_question_itself(client, sync_engine, state):
    # A reservation counts as seen even if it was never answered or never
    # shown, so the partner is dropped; the question itself stays servable.
    experiment = _create_experiment(client)
    _upload(client, experiment["id"], KEYED_CSV)
    ids = _row_ids(sync_engine, experiment["id"])
    session = _start_session(client, experiment["id"])
    _seed_reservation(sync_engine, session["rater_id"], ids["a"], state)

    assert _eligible(client, experiment["id"], session["rater_id"]) == {"a", "c", "d"}


def test_another_raters_history_does_not_drop_anything(client, sync_engine):
    experiment = _create_experiment(client)
    _upload(client, experiment["id"], KEYED_CSV)
    ids = _row_ids(sync_engine, experiment["id"])
    other = _start_session(client, experiment["id"], prolific_pid="PID_OTHER")
    _seed_rating(sync_engine, other["rater_id"], ids["a"])
    _seed_reservation(sync_engine, other["rater_id"], ids["d"], "expired")
    session = _start_session(client, experiment["id"])

    assert _eligible(client, experiment["id"], session["rater_id"]) == {"a", "b", "c", "d"}


def test_rater_is_served_one_question_per_key_until_exhausted(client, sync_engine):
    experiment = _create_experiment(client)
    _upload(client, experiment["id"], KEYED_CSV)
    names = {row_id: name for name, row_id in _row_ids(sync_engine, experiment["id"]).items()}
    session = _start_session(client, experiment["id"])

    served = []
    while (question := _get_next_question(client, session)) is not None:
        served.append(names[question["id"]])
        _submit(client, session, question)

    assert len(served) == 3
    assert {"c", "d"} <= set(served)
    assert len({"a", "b"} & set(served)) == 1


def test_expired_reservation_of_the_question_itself_is_served_again(client, sync_engine):
    experiment = _create_experiment(client)
    _upload(
        client,
        experiment["id"],
        "question_id,question_text,exclusion_key\n"
        "a,Vulnerable version?,commit-1\n"
        "b,Patched version?,commit-1\n",
    )
    ids = _row_ids(sync_engine, experiment["id"])
    session = _start_session(client, experiment["id"])
    _seed_reservation(sync_engine, session["rater_id"], ids["a"], "expired")

    question = _get_next_question(client, session)
    assert question is not None and question["id"] == ids["a"]
    _submit(client, session, question)
    assert _get_next_question(client, session) is None


def test_sibling_preference_does_not_serve_an_excluded_sibling(client, sync_engine):
    # Without the exclusion, having rated p1 would keep the rater in parent P
    # and serve p2 next.
    experiment = _create_experiment(client)
    _upload(
        client,
        experiment["id"],
        "question_id,question_text,parent_question_id,exclusion_key\n"
        'P,"Shared function source",,\n'
        "p1,Is the vulnerable version exploitable?,P,commit-1\n"
        "p2,Is the patched version exploitable?,P,commit-1\n"
        "s,Unrelated?,,\n",
    )
    ids = _row_ids(sync_engine, experiment["id"])
    session = _start_session(client, experiment["id"])
    _seed_rating(sync_engine, session["rater_id"], ids["p1"])

    question = _get_next_question(client, session)
    assert question is not None and question["id"] == ids["s"]
    _submit(client, session, question)
    assert _get_next_question(client, session) is None


def test_queue_does_not_reserve_the_partner_behind_its_head(client, monkeypatch, sync_engine):
    monkeypatch.setattr(
        "services.assistance.methods.top_n.TopNAssistance.start",
        AsyncMock(return_value=InteractionStep(type=StepType.DISPLAY, is_terminal=True)),
    )
    experiment = _create_experiment(client)
    _upload(
        client,
        experiment["id"],
        "question_id,question_text,exclusion_key\n"
        "a,Vulnerable version?,commit-1\n"
        "b,Patched version?,commit-1\n",
    )
    response = client.patch(
        f"/api/admin/experiments/{experiment['id']}",
        json={
            "assistance_method": "top_n",
            "assistance_params": {
                "assistance_models": {
                    "top_n": {
                        "model": "openrouter/test",
                        "reasoning_effort": None,
                        "text_verbosity": None,
                        "temperature": None,
                    }
                }
            },
        },
    )
    assert response.status_code == 200, response.text
    monkeypatch.setattr(get_settings().prefetch, "experiment_ids", [experiment["id"]])
    session = _start_session(client, experiment["id"])

    response = client.post(
        "/api/raters/queue",
        headers={"X-Rater-Session": session["rater_session_token"]},
        json={},
    )
    assert response.status_code == 200, response.text
    # Room for a successor, but the only candidate shares the head's key.
    assert len(response.json()["items"]) == 1
