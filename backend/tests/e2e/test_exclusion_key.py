"""A rater is served at most one of the questions sharing an exclusion_key (#157)."""

from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient
from sqlalchemy import text

from test_characterization import (
    _build_parquet_bytes,
    _create_experiment,
    _start_session,
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
