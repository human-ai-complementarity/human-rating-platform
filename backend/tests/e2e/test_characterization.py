from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import ExperimentRound
from services.participant_groups import _slugify_for_prolific

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _create_experiment(client: TestClient, *, completion_url: str | None = None) -> dict:
    response = client.post(
        "/api/admin/experiments",
        json={
            "name": _unique_name("experiment"),
            "num_ratings_per_question": 2,
            "prolific_completion_url": completion_url,
        },
    )
    assert response.status_code == 200
    return response.json()


def _mark_experiment_status(sync_engine, experiment_id: int, status: str) -> None:
    """Force an experiment's `status` column for tests that need to bypass
    the natural lifecycle (e.g. setting up a FINISHED experiment to reference
    as an exclusion target without running the full pilot -> launch -> close
    -> finish sequence)."""
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET status = :status WHERE id = :id"),
            {"status": status, "id": experiment_id},
        )


def _insert_round(
    sync_engine,
    *,
    experiment_id: int,
    round_number: int,
    status: str = "COMPLETED",
    total_cost: int | None = None,
) -> None:
    """Insert an experiment round directly, for tests that need controlled
    round state (status / Prolific total_cost) without driving the Prolific
    create/publish/close flow."""
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO experiment_rounds (
                    experiment_id, round_number, prolific_study_id,
                    prolific_study_status, description, estimated_completion_time,
                    reward, device_compatibility, places_requested, total_cost
                ) VALUES (
                    :experiment_id, :round_number, :study_id, :status, :description,
                    :ect, :reward, :device, :places, :total_cost
                )
                """
            ),
            {
                "experiment_id": experiment_id,
                "round_number": round_number,
                "study_id": f"STUDY_{experiment_id}_{round_number}",
                "status": status,
                "description": "seeded round",
                "ect": 10,
                "reward": 100,
                "device": '["desktop"]',
                "places": 3,
                "total_cost": total_cost,
            },
        )


def _upload_questions(client: TestClient, experiment_id: int) -> None:
    csv_data = (
        "question_id,question_text,gt_answer,options,question_type\n"
        "q1,Is this useful?,Yes,Yes|No,MC\n"
        "q2,Explain why,,,"
    )
    response = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("questions.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 200


def _start_session(client: TestClient, experiment_id: int, prolific_pid: str = "PID_A") -> dict:
    response = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment_id,
            "PROLIFIC_PID": prolific_pid,
            "STUDY_ID": "STUDY_1",
            "SESSION_ID": f"SESSION_{prolific_pid}",
        },
    )
    assert response.status_code == 200
    return response.json()


def _rater_headers(session_payload: dict) -> dict[str, str]:
    return {"X-Rater-Session": session_payload["rater_session_token"]}


def _seed_export_dataset(sync_engine, experiment_id: int, row_count: int) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO questions (
                    experiment_id,
                    question_id,
                    question_text,
                    gt_answer,
                    options,
                    question_type,
                    extra_data
                )
                SELECT
                    :experiment_id,
                    CONCAT('bulk-', gs::text),
                    CONCAT('Bulk question ', gs::text),
                    '',
                    '',
                    'MC',
                    '{}'
                FROM generate_series(1, :row_count) AS gs
                """
            ),
            {"experiment_id": experiment_id, "row_count": row_count},
        )

        rater_id = conn.execute(
            text(
                """
                INSERT INTO raters (
                    prolific_id,
                    study_id,
                    session_id,
                    experiment_id,
                    session_start,
                    is_active
                )
                VALUES (
                    'PID_EXPORT',
                    'STUDY_EXPORT',
                    'SESSION_EXPORT',
                    :experiment_id,
                    NOW(),
                    true
                )
                RETURNING id
                """
            ),
            {"experiment_id": experiment_id},
        ).scalar_one()

        conn.execute(
            text(
                """
                INSERT INTO ratings (
                    question_id,
                    rater_id,
                    answer,
                    confidence,
                    time_started,
                    time_submitted
                )
                SELECT
                    q.id,
                    :rater_id,
                    'Yes',
                    3,
                    NOW(),
                    NOW()
                FROM questions q
                WHERE q.experiment_id = :experiment_id
                """
            ),
            {"experiment_id": experiment_id, "rater_id": rater_id},
        )


def test_health_endpoint_smoke(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert "version" in body
    assert "commit" in body


def test_list_experiments_returns_empty_list_initially(client: TestClient):
    response = client.get("/api/admin/experiments")
    assert response.status_code == 200
    assert response.json() == []


def test_create_experiment_then_list_contains_it(client: TestClient):
    created = _create_experiment(client)

    response = client.get("/api/admin/experiments")
    assert response.status_code == 200
    items = response.json()

    assert len(items) == 1
    assert items[0]["id"] == created["id"]
    assert items[0]["question_count"] == 0
    assert items[0]["rating_count"] == 0


def test_new_experiment_is_not_archived(client: TestClient):
    created = _create_experiment(client)
    assert created["archived_at"] is None


def test_archive_hides_from_default_list_and_shows_under_archived(client: TestClient):
    keep = _create_experiment(client)
    target = _create_experiment(client)

    archive = client.post(f"/api/admin/experiments/{target['id']}/archive")
    assert archive.status_code == 200
    assert archive.json()["archived_at"] is not None

    active = client.get("/api/admin/experiments").json()
    active_ids = {item["id"] for item in active}
    assert target["id"] not in active_ids
    assert keep["id"] in active_ids

    archived = client.get("/api/admin/experiments", params={"archived": True}).json()
    archived_ids = {item["id"] for item in archived}
    assert archived_ids == {target["id"]}


def test_unarchive_returns_experiment_to_active_list(client: TestClient):
    target = _create_experiment(client)
    client.post(f"/api/admin/experiments/{target['id']}/archive")

    unarchive = client.post(f"/api/admin/experiments/{target['id']}/unarchive")
    assert unarchive.status_code == 200
    assert unarchive.json()["archived_at"] is None

    active_ids = {item["id"] for item in client.get("/api/admin/experiments").json()}
    assert target["id"] in active_ids
    archived = client.get("/api/admin/experiments", params={"archived": True}).json()
    assert archived == []


def test_get_experiment_returns_single_by_id(client: TestClient):
    created = _create_experiment(client)

    response = client.get(f"/api/admin/experiments/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["question_count"] == 0
    assert body["rating_count"] == 0


def test_get_experiment_resolves_archived_experiment(client: TestClient):
    created = _create_experiment(client)
    client.post(f"/api/admin/experiments/{created['id']}/archive")

    # The list hides archived rows, but a direct fetch must still resolve one
    # so the detail page can open it.
    response = client.get(f"/api/admin/experiments/{created['id']}")
    assert response.status_code == 200
    assert response.json()["archived_at"] is not None


def test_get_experiment_missing_returns_404(client: TestClient):
    response = client.get("/api/admin/experiments/999999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Experiment not found"


def test_list_filters_by_status(client: TestClient, sync_engine):
    draft = _create_experiment(client)
    launched = _create_experiment(client)
    _mark_experiment_status(sync_engine, launched["id"], "LAUNCH")

    launch_only = client.get("/api/admin/experiments", params={"status": "LAUNCH"}).json()
    assert {item["id"] for item in launch_only} == {launched["id"]}

    draft_only = client.get("/api/admin/experiments", params={"status": "DRAFT"}).json()
    assert {item["id"] for item in draft_only} == {draft["id"]}


def test_list_filters_by_name_search(client: TestClient):
    match = client.post(
        "/api/admin/experiments",
        json={"name": "Factuality study", "num_ratings_per_question": 2},
    ).json()
    client.post(
        "/api/admin/experiments",
        json={"name": "Sentiment study", "num_ratings_per_question": 2},
    )

    # Case-insensitive substring match on the public name.
    hits = client.get("/api/admin/experiments", params={"search": "factual"}).json()
    assert {item["id"] for item in hits} == {match["id"]}


def test_list_search_matches_internal_name(client: TestClient):
    match = client.post(
        "/api/admin/experiments",
        json={
            "name": "Public label",
            "internal_name": "Q3 pilot — Sander",
            "num_ratings_per_question": 2,
        },
    ).json()
    client.post(
        "/api/admin/experiments",
        json={"name": "Another public label", "num_ratings_per_question": 2},
    )

    hits = client.get("/api/admin/experiments", params={"search": "q3 pilot"}).json()
    assert {item["id"] for item in hits} == {match["id"]}


def test_list_include_archived_returns_active_and_archived(client: TestClient):
    active = _create_experiment(client)
    archived = _create_experiment(client)
    assert client.post(f"/api/admin/experiments/{archived['id']}/archive").status_code == 200

    # include_archived returns both, unlike the default (active only) or the
    # archived=true filter (archived only).
    both = client.get("/api/admin/experiments", params={"include_archived": True}).json()
    assert {item["id"] for item in both} == {active["id"], archived["id"]}

    default = client.get("/api/admin/experiments").json()
    assert {item["id"] for item in default} == {active["id"]}


def test_list_spend_sums_round_total_cost(client: TestClient, sync_engine):
    exp = _create_experiment(client)
    _insert_round(sync_engine, experiment_id=exp["id"], round_number=0, total_cost=620)
    _insert_round(sync_engine, experiment_id=exp["id"], round_number=1, total_cost=240)
    # A round Prolific has not costed yet (NULL total_cost) contributes nothing.
    _insert_round(sync_engine, experiment_id=exp["id"], round_number=2, total_cost=None)

    item = next(i for i in client.get("/api/admin/experiments").json() if i["id"] == exp["id"])
    assert item["spend_minor_units"] == 860


def test_list_spend_zero_without_synced_rounds(client: TestClient):
    exp = _create_experiment(client)
    item = next(i for i in client.get("/api/admin/experiments").json() if i["id"] == exp["id"])
    assert item["spend_minor_units"] == 0


def test_upload_questions_records_upload_and_stats(client: TestClient):
    experiment = _create_experiment(client)
    experiment_id = experiment["id"]

    _upload_questions(client, experiment_id)

    uploads_response = client.get(f"/api/admin/experiments/{experiment_id}/uploads")
    stats_response = client.get(f"/api/admin/experiments/{experiment_id}/stats")

    assert uploads_response.status_code == 200
    assert len(uploads_response.json()) == 1
    assert uploads_response.json()[0]["question_count"] == 2

    assert stats_response.status_code == 200
    stats_payload = stats_response.json()
    assert stats_payload["total_questions"] == 2
    assert stats_payload["total_ratings"] == 0


def test_upload_rejects_non_csv_file(client: TestClient):
    experiment = _create_experiment(client)

    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("questions.txt", "nope", "text/plain")},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "csv" in detail and "parquet" in detail


def _csv_with_meta(meta: dict | str) -> str:
    meta_str = meta if isinstance(meta, str) else json.dumps(meta)
    return (
        f"#META: {meta_str}\n"
        "question_id,question_text,gt_answer,options,question_type\n"
        "q1,Is this useful?,Yes,Yes|No,MC\n"
        "q2,Explain why,,,\n"
    )


def _fetch_experiment(client: TestClient, experiment_id: int) -> dict:
    # No GET-by-id endpoint exists; pull the list and pick ours out.
    items = client.get("/api/admin/experiments").json()
    return next(item for item in items if item["id"] == experiment_id)


def test_upload_with_meta_header_populates_experiment(client: TestClient):
    experiment = _create_experiment(client)
    meta = {
        "description": "Pilot dataset on x.",
        "system_prompt": "You are an evaluator.",
        "human_prompt_prefix": "When you see the text below, do you think x or y?",
        "human_prompt_suffix": "Pick the option that best matches.",
        "prolific_pool": "uk_representative_sample",
    }

    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("with_meta.csv", _csv_with_meta(meta), "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["meta_applied"]) == sorted(meta.keys())
    assert body["meta_conflicts"] == []

    refreshed = _fetch_experiment(client, experiment["id"])
    for key, value in meta.items():
        assert refreshed[key] == value

    uploads = client.get(f"/api/admin/experiments/{experiment['id']}/uploads").json()
    assert uploads[0]["dataset_meta"] == meta


def test_upload_second_csv_with_differing_meta_keeps_existing(client: TestClient):
    experiment = _create_experiment(client)
    client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("first.csv", _csv_with_meta({"description": "first"}), "text/csv")},
    )
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("second.csv", _csv_with_meta({"description": "second"}), "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    # Second upload's value disagreed — reported as a conflict, not applied.
    assert body["meta_applied"] == []
    assert body["meta_conflicts"] == ["description"]

    refreshed = _fetch_experiment(client, experiment["id"])
    assert refreshed["description"] == "first"


def test_upload_second_csv_with_identical_meta_reports_neither_applied_nor_conflict(
    client: TestClient,
):
    # Re-declaring the same value the experiment already has is a no-op: not a
    # conflict (nothing disagrees) and not "applied" (nothing was written).
    experiment = _create_experiment(client)
    client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("first.csv", _csv_with_meta({"description": "same"}), "text/csv")},
    )
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("second.csv", _csv_with_meta({"description": "same"}), "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["meta_applied"] == []
    assert body["meta_conflicts"] == []


def test_upload_handles_utf8_bom_in_csv(client: TestClient):
    # Excel "Save As CSV UTF-8" and pandas' `encoding="utf-8-sig"` both emit a
    # leading BOM. The parser must consume it so the `#META:` line is recognised.
    experiment = _create_experiment(client)
    csv_body = "\ufeff" + _csv_with_meta({"description": "from bom file"})
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("bom.csv", csv_body.encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 200
    assert response.json()["meta_applied"] == ["description"]
    refreshed = _fetch_experiment(client, experiment["id"])
    assert refreshed["description"] == "from bom file"


def test_upload_rejects_invalid_meta_json(client: TestClient):
    experiment = _create_experiment(client)
    csv_body = "#META: not json at all\nquestion_id,question_text\nq1,hello\n"
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("bad.csv", csv_body, "text/csv")},
    )
    assert response.status_code == 400
    assert "Invalid #META: JSON" in response.json()["detail"]


def test_upload_rejects_unknown_meta_keys(client: TestClient):
    experiment = _create_experiment(client)
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("bad.csv", _csv_with_meta({"description": "ok", "wat": 1}), "text/csv")},
    )
    assert response.status_code == 400
    assert "Unknown dataset metadata keys" in response.json()["detail"]
    assert "wat" in response.json()["detail"]


def test_upload_without_meta_header_still_works(client: TestClient):
    # Backwards-compat: legacy CSVs without a #META: line must parse exactly as before.
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    refreshed = _fetch_experiment(client, experiment["id"])
    assert refreshed["description"] is None
    assert refreshed["system_prompt"] is None
    uploads = client.get(f"/api/admin/experiments/{experiment['id']}/uploads").json()
    assert uploads[0]["dataset_meta"] is None


def test_rater_session_exposes_prefix_and_suffix(client: TestClient):
    # Prefix + suffix travel with the rater session (constant for the session,
    # not per-question) so the question-fetch payload stays small.
    experiment = _create_experiment(client)
    client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={
            "file": (
                "with_meta.csv",
                _csv_with_meta(
                    {
                        "human_prompt_prefix": "When you see the text below, do you think x or y?",
                        "human_prompt_suffix": "Pick the option that best matches.",
                    }
                ),
                "text/csv",
            )
        },
    )
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_HP")
    assert (
        session_payload["human_prompt_prefix"]
        == "When you see the text below, do you think x or y?"
    )
    assert session_payload["human_prompt_suffix"] == "Pick the option that best matches."


def test_rater_session_prefix_suffix_null_when_unset(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_NO_HP")
    assert session_payload["human_prompt_prefix"] is None
    assert session_payload["human_prompt_suffix"] is None


def test_rater_session_renders_description_markdown_to_html(client: TestClient):
    # Markdown in `description` is converted to the same HTML subset Prolific
    # accepts before it reaches the splash, so what raters see matches the
    # Prolific external listing.
    experiment = _create_experiment(client)
    client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={
            "file": (
                "with_meta.csv",
                _csv_with_meta({"description": "# Heading\n\n**Bold** paragraph."}),
                "text/csv",
            )
        },
    )
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_MD")
    html = session_payload["experiment_description_html"]
    assert html is not None
    assert "<h1>Heading</h1>" in html
    assert "<b>Bold</b>" in html


def test_update_experiment_edits_dataset_metadata(client: TestClient):
    experiment = _create_experiment(client)
    response = client.patch(
        f"/api/admin/experiments/{experiment['id']}",
        json={
            "assistance_method": "none",
            "description": "Manually set",
            "system_prompt": "Be precise.",
            "prolific_pool": "us_balanced",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Manually set"
    assert body["system_prompt"] == "Be precise."
    assert body["prolific_pool"] == "us_balanced"
    assert body["human_prompt_prefix"] is None  # untouched fields stay null
    assert body["human_prompt_suffix"] is None


def test_update_experiment_edits_public_name(client: TestClient):
    experiment = _create_experiment(client)
    response = client.patch(
        f"/api/admin/experiments/{experiment['id']}",
        json={"assistance_method": "none", "name": "  Renamed task  "},
    )
    assert response.status_code == 200, response.text
    # Whitespace is trimmed, mirroring the create path.
    assert response.json()["name"] == "Renamed task"


def test_update_experiment_rejects_empty_public_name(client: TestClient):
    experiment = _create_experiment(client)
    response = client.patch(
        f"/api/admin/experiments/{experiment['id']}",
        json={"assistance_method": "none", "name": "   "},
    )
    assert response.status_code == 400
    assert "Public name" in response.json()["detail"]


def test_update_experiment_clears_internal_name_with_empty_string(client: TestClient):
    experiment = _create_experiment(client)
    client.patch(
        f"/api/admin/experiments/{experiment['id']}",
        json={"assistance_method": "none", "internal_name": "Working title"},
    )
    response = client.patch(
        f"/api/admin/experiments/{experiment['id']}",
        json={"assistance_method": "none", "internal_name": ""},
    )
    assert response.status_code == 200, response.text
    assert response.json()["internal_name"] is None


# ── Parquet upload ───────────────────────────────────────────────────────────
# Parquet shares the upload endpoint, the meta-conflict logic and the Question
# writer with CSV — the tests below check the format-specific bits: schema
# metadata parsing, typed-column serialisation, and extension dispatch.


def _build_parquet_bytes(
    *,
    rows: list[dict] | None = None,
    meta: dict | None = None,
) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    if rows is None:
        rows = [
            {"question_id": "q1", "question_text": "Is this useful?", "question_type": "MC"},
            {"question_id": "q2", "question_text": "Explain why", "question_type": "FT"},
        ]
    table = pa.Table.from_pylist(rows)
    if meta is not None:
        merged = {
            **(table.schema.metadata or {}),
            b"dataset_meta": json.dumps(meta).encode("utf-8"),
        }
        table = table.replace_schema_metadata(merged)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def test_upload_parquet_with_dataset_meta_populates_experiment(client: TestClient):
    experiment = _create_experiment(client)
    meta = {
        "description": "Parquet pilot",
        "system_prompt": "You are an evaluator.",
        "human_prompt_prefix": "When you see the text below, do you think x or y?",
        "prolific_pool": "uk_representative_sample",
    }
    parquet_bytes = _build_parquet_bytes(meta=meta)

    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("with_meta.parquet", parquet_bytes, "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["meta_applied"]) == sorted(meta.keys())
    assert body["meta_conflicts"] == []

    refreshed = _fetch_experiment(client, experiment["id"])
    for key, value in meta.items():
        assert refreshed[key] == value


def test_upload_parquet_without_meta_still_works(client: TestClient):
    experiment = _create_experiment(client)
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("no_meta.parquet", _build_parquet_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 200
    assert response.json()["meta_applied"] == []

    refreshed = _fetch_experiment(client, experiment["id"])
    assert refreshed["description"] is None
    uploads = client.get(f"/api/admin/experiments/{experiment['id']}/uploads").json()
    assert uploads[0]["dataset_meta"] is None


def test_upload_parquet_serialises_list_options_and_dict_metadata(client: TestClient):
    # The colab notebook writes `options` as a typed list and `metadata` as a
    # struct. The reader must produce the same DB string forms a CSV upload
    # would: pipe-joined options and JSON-encoded metadata.
    experiment = _create_experiment(client)
    rows = [
        {
            "question_id": "q1",
            "question_text": "Is this useful?",
            "gt_answer": "Yes",
            "options": ["Yes", "No"],
            "question_type": "MC",
            "metadata": {"topic": "x", "difficulty": 2},
        }
    ]
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={
            "file": (
                "typed.parquet",
                _build_parquet_bytes(rows=rows),
                "application/octet-stream",
            )
        },
    )
    assert response.status_code == 200, response.text

    session = _start_session(client, experiment["id"], prolific_pid="PID_PQ")
    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session),
    ).json()
    # Confirms the canonical pipe-separated form reached the rater payload.
    assert question["options"] == "Yes|No"


def test_upload_parquet_rejects_unknown_meta_keys(client: TestClient):
    experiment = _create_experiment(client)
    parquet_bytes = _build_parquet_bytes(meta={"description": "ok", "wat": 1})
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("bad.parquet", parquet_bytes, "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "wat" in response.json()["detail"]


def test_upload_parquet_missing_required_column(client: TestClient):
    experiment = _create_experiment(client)
    rows = [{"question_id": "q1"}]  # no question_text
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={
            "file": (
                "bad.parquet",
                _build_parquet_bytes(rows=rows),
                "application/octet-stream",
            )
        },
    )
    assert response.status_code == 400
    assert "question_text" in response.json()["detail"]


def test_upload_rejects_unsupported_extension(client: TestClient):
    # Only `.csv` and `.parquet` are accepted; anything else gets a clean 400.
    experiment = _create_experiment(client)
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("questions.json", "{}", "application/json")},
    )
    assert response.status_code == 400
    assert "csv" in response.json()["detail"]
    assert "parquet" in response.json()["detail"]


def test_upload_accepts_large_question_text_fields(client: TestClient):
    experiment = _create_experiment(client)
    large_question_text = "x" * 200_000

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["question_id", "question_text", "gt_answer", "options", "question_type"])
    writer.writerow(["long-q", large_question_text, "Yes", "Yes|No", "MC"])

    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("long_questions.csv", output.getvalue(), "text/csv")},
    )

    assert response.status_code == 200
    assert response.json()["message"] == "Uploaded 1 questions"

    stats_response = client.get(f"/api/admin/experiments/{experiment['id']}/stats")
    assert stats_response.status_code == 200
    assert stats_response.json()["total_questions"] == 1


def test_start_session_creates_new_rater_session(client: TestClient):
    experiment = _create_experiment(
        client,
        completion_url="https://app.prolific.com/submissions/complete?cc=ABCD1234",
    )

    payload = _start_session(client, experiment["id"], prolific_pid="PID_1")

    assert payload["rater_id"] > 0
    assert payload["experiment_name"] == experiment["name"]
    assert payload["completion_url"].startswith("https://app.prolific.com/")


def test_start_session_twice_resumes_same_active_session(client: TestClient):
    experiment = _create_experiment(client)

    first = _start_session(client, experiment["id"], prolific_pid="PID_RESUME")
    second = _start_session(client, experiment["id"], prolific_pid="PID_RESUME")

    assert first["rater_id"] == second["rater_id"]
    assert first["session_start"] == second["session_start"]


def test_start_session_rejects_after_end_session(client: TestClient):
    experiment = _create_experiment(client)
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_DONE")

    end_response = client.post(
        "/api/raters/end-session",
        headers=_rater_headers(session_payload),
    )
    restart_response = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment["id"],
            "PROLIFIC_PID": "PID_DONE",
            "STUDY_ID": "STUDY_1",
            "SESSION_ID": "SESSION_PID_DONE_RESTART",
        },
    )

    assert end_response.status_code == 200
    assert restart_response.status_code == 403


def test_next_question_returns_eligible_question(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_NEXT")

    response = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["question_id"] in {"q1", "q2"}


def test_submit_rating_success_then_duplicate_rejected(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_SUBMIT")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    submit_payload = {
        "question_id": question["id"],
        "answer": "Yes",
        "confidence": 4,
        "time_started": datetime.now(UTC).isoformat(),
    }

    first = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json=submit_payload,
    )
    duplicate = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json=submit_payload,
    )

    assert first.status_code == 200
    assert first.json()["success"] is True
    assert duplicate.status_code == 400


def test_submit_rating_rejects_invalid_confidence(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_CONF")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    response = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 9,
            "time_started": datetime.now(UTC).isoformat(),
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(error["loc"][-1] == "confidence" for error in detail)


def test_session_status_reflects_completed_questions(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_STATUS")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()
    client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "No",
            "confidence": 3,
            "time_started": datetime.now(UTC).isoformat(),
        },
    )

    response = client.get(
        "/api/raters/session-status",
        headers=_rater_headers(session_payload),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["questions_completed"] == 1
    assert payload["time_remaining_seconds"] > 0


def test_next_question_marks_expired_session_inactive(
    client: TestClient,
    backdate_rater_session,
):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_EXPIRED")

    backdate_rater_session(session_payload["rater_id"])

    expired_response = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    )
    status_response = client.get(
        "/api/raters/session-status",
        headers=_rater_headers(session_payload),
    )

    assert expired_response.status_code == 403
    assert expired_response.json()["detail"] == "Session expired"
    assert status_response.status_code == 200
    assert status_response.json()["is_active"] is False


def test_export_ratings_streams_large_dataset_in_chunks(client: TestClient, sync_engine):
    settings = get_settings()
    row_count = settings.testing.export_seed_row_count
    experiment = _create_experiment(client)
    _seed_export_dataset(sync_engine, experiment["id"], row_count=row_count)

    with client.stream("GET", f"/api/admin/experiments/{experiment['id']}/export") as response:
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        chunks = list(response.iter_text())

    assert len(chunks) >= 1

    parsed_rows = list(csv.reader(io.StringIO("".join(chunks))))
    assert parsed_rows[0][0] == "rating_id"
    assert len(parsed_rows) == row_count + 1


def test_analytics_endpoint_returns_expected_payload_shape(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_ANALYTICS")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    submit_response = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": datetime.now(UTC).isoformat(),
        },
    )
    assert submit_response.status_code == 200

    analytics_response = client.get(f"/api/admin/experiments/{experiment['id']}/analytics")
    assert analytics_response.status_code == 200

    payload = analytics_response.json()
    overview = payload["overview"]
    assert payload["experiment_name"] == experiment["name"]
    assert overview["total_questions"] == 2
    assert overview["total_ratings"] == 1
    assert overview["total_raters"] == 1
    assert isinstance(payload["questions"], list) and len(payload["questions"]) == 1
    assert isinstance(payload["raters"], list) and len(payload["raters"]) == 1
    assert payload["questions"][0]["answer_distribution"] == {"Yes": 1}


def test_migration_runner_current_and_history_commands_succeed():
    revision_pattern = re.compile(r'^revision:\s*str\s*=\s*"([^"]+)"', re.MULTILINE)
    down_pattern = re.compile(r"^down_revision:\s*.*=\s*(.+)$", re.MULTILINE)
    revisions: set[str] = set()
    down_revisions: set[str] = set()

    for path in (BACKEND_DIR / "alembic" / "versions").glob("*.py"):
        if path.name == "__init__.py":
            continue
        content = path.read_text()
        revision_match = revision_pattern.search(content)
        assert revision_match is not None
        revisions.add(revision_match.group(1))

        down_match = down_pattern.search(content)
        assert down_match is not None
        down_raw = down_match.group(1).strip()
        if down_raw == "None":
            continue
        for revision in re.findall(r'"([^"]+)"', down_raw):
            down_revisions.add(revision)

    assert revisions
    head_revisions = sorted(revisions - down_revisions)
    assert head_revisions

    current = subprocess.run(
        ["sh", "scripts/migrate.sh", "current"],
        cwd=BACKEND_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    history = subprocess.run(
        ["sh", "scripts/migrate.sh", "history"],
        cwd=BACKEND_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert current.returncode == 0
    assert history.returncode == 0

    current_output = f"{current.stdout}\n{current.stderr}"
    history_output = f"{history.stdout}\n{history.stderr}"
    assert "(head)" in current_output
    assert any(rev in current_output for rev in head_revisions)
    for revision_id in revisions:
        assert revision_id in history_output


def test_app_creation_succeeds_with_default_env():
    env = os.environ.copy()
    env.setdefault("APP_SECRET_KEY", "test-secret")

    result = subprocess.run(
        [sys.executable, "-c", "from main import create_app; create_app(); print('ok')"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "ok" in result.stdout


# ── Prolific integration tests ──────────────────────────────────────────────
# These use respx to mock outbound httpx calls to the Prolific API, verifying
# the full path: TestClient → FastAPI → service → Prolific client → mock.

PROLIFIC_BASE = "https://api.prolific.com/api/v1"
PROLIFIC_STUDY_ID = "65abc123def456"


@pytest.fixture()
def enable_prolific():
    """Temporarily enable Prolific by setting an API token on the cached settings.

    Also sets a project_id since participant-group creation requires it; without
    this the fixture is non-hermetic and passes locally only when the dev .env
    happens to set PROLIFIC__PROJECT_ID.
    """
    settings = get_settings()
    original_token = settings.prolific.api_token
    original_project_id = settings.prolific.project_id
    settings.prolific.api_token = "test-token"
    settings.prolific.project_id = "test-project"
    yield settings
    settings.prolific.api_token = original_token
    settings.prolific.project_id = original_project_id


def _prolific_experiment_payload() -> dict:
    return {
        "name": _unique_name("prolific-exp"),
        "num_ratings_per_question": 2,
        "prolific_completion_url": None,
    }


def _pilot_payload() -> dict:
    return {
        "description": "Test study",
        "estimated_completion_time": 10,
        "reward": 500,
        "pilot_places": 5,
        "device_compatibility": ["desktop"],
    }


def _mock_create_study(*, status: int = 200, study_id: str = PROLIFIC_STUDY_ID) -> respx.Route:
    body = {"id": study_id, "status": "UNPUBLISHED"} if status == 200 else {}
    # Every round launch (including pilot) now ensures the experiment's own
    # participant group exists — attach a default mock so tests that don't
    # care about groups don't have to register one explicitly.
    _install_default_participant_group_mock()
    return respx.post(f"{PROLIFIC_BASE}/studies/").mock(return_value=Response(status, json=body))


def _install_default_participant_group_mock() -> respx.Route:
    """Register a side-effect mock that echoes each request's `name` field as
    the returned group ID. Lets tests that need to distinguish groups derive
    expected IDs from the experiment they belong to."""

    def responder(request):
        body = json.loads(request.content.decode())
        name = body.get("name", "test-group")
        return Response(200, json={"id": name, "name": name, "project_id": "p"})

    return respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(side_effect=responder)


def _mock_publish_study(*, study_id: str = PROLIFIC_STUDY_ID) -> respx.Route:
    return respx.post(f"{PROLIFIC_BASE}/studies/{study_id}/transition/").mock(
        return_value=Response(200, json={"id": study_id, "status": "ACTIVE"})
    )


def _mock_close_study(
    *,
    study_id: str = PROLIFIC_STUDY_ID,
    closed_status: str = "AWAITING_REVIEW",
) -> respx.Route:
    return respx.post(f"{PROLIFIC_BASE}/studies/{study_id}/transition/").mock(
        return_value=Response(200, json={"id": study_id, "status": closed_status})
    )


def _mock_delete_study(*, study_id: str = PROLIFIC_STUDY_ID, status: int = 204) -> respx.Route:
    body = {} if status == 204 else {"error": "fail"}
    return respx.delete(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=Response(status, json=body)
    )


def _mock_get_study(
    *,
    study_id: str = PROLIFIC_STUDY_ID,
    study_status: str = "ACTIVE",
    total_cost: int | None = None,
    status: int = 200,
) -> respx.Route:
    body = {"id": study_id, "status": study_status} if status == 200 else {"error": "fail"}
    if status == 200 and total_cost is not None:
        body["total_cost"] = total_cost
    return respx.get(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=Response(status, json=body)
    )


def _mock_project(
    *,
    project_id: str,
    workspace_id: str | None = "WS_ABC",
    status: int = 200,
) -> respx.Route:
    if status == 200:
        body: dict = {"id": project_id}
        if workspace_id is not None:
            body["workspace"] = workspace_id
    else:
        body = {}
    return respx.get(f"{PROLIFIC_BASE}/projects/{project_id}/").mock(
        return_value=Response(status, json=body)
    )


def _mock_workspace_balance(
    *,
    workspace_id: str,
    currency_code: str = "USD",
    status: int = 200,
) -> respx.Route:
    body = (
        {
            "currency_code": currency_code,
            "total_balance": 0,
            "available_balance": 0,
        }
        if status == 200
        else {}
    )
    return respx.get(f"{PROLIFIC_BASE}/workspaces/{workspace_id}/balance/").mock(
        return_value=Response(status, json=body)
    )


def _mock_update_study(
    *,
    study_id: str = PROLIFIC_STUDY_ID,
    status: int = 200,
) -> respx.Route:
    body = {"id": study_id, "status": "UNPUBLISHED"} if status == 200 else {"error": "fail"}
    return respx.patch(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=Response(status, json=body)
    )


@pytest.fixture(autouse=True)
def _reset_prolific_currency_cache():
    # Module-level cache in services.admin.prolific persists across tests in
    # the same process; reset it so each test sees a clean lookup state.
    from services.admin.prolific import _reset_currency_cache

    _reset_currency_cache()
    yield
    _reset_currency_cache()


def _patch_commit_to_fail_for_round(
    monkeypatch: pytest.MonkeyPatch,
    *,
    round_number: int,
) -> None:
    original_commit = AsyncSession.commit
    state = {"failed": False}

    async def failing_commit(self: AsyncSession, *args, **kwargs):
        pending_rounds = [
            obj
            for obj in self.sync_session.new
            if isinstance(obj, ExperimentRound) and obj.round_number == round_number
        ]
        if pending_rounds and not state["failed"]:
            state["failed"] = True
            raise IntegrityError(
                "forced experiment_round conflict",
                params=None,
                orig=Exception("forced experiment_round conflict"),
            )
        return await original_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)


def _create_prolific_experiment(client: TestClient) -> tuple[dict, dict]:
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    return experiment, pilot_resp.json()


@respx.mock
def test_prolific_create_stores_study_id(client: TestClient, enable_prolific):
    experiment, pilot = _create_prolific_experiment(client)

    assert pilot["prolific_study_id"] == PROLIFIC_STUDY_ID
    assert pilot["prolific_study_status"] == "UNPUBLISHED"
    assert pilot["prolific_study_url"] is not None
    assert PROLIFIC_STUDY_ID in pilot["prolific_study_url"]

    experiments = client.get("/api/admin/experiments").json()
    stored = next(item for item in experiments if item["id"] == experiment["id"])
    assert stored["prolific_completion_url"] is not None
    rounds = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/rounds").json()
    assert [round_["round_number"] for round_ in rounds] == [0]


@respx.mock
def test_prolific_round_names_include_round_label(client: TestClient, enable_prolific):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    pilot_route = _mock_create_study(study_id="PILOT_STUDY")
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    assert pilot_route.called
    pilot_payload = json.loads(pilot_route.calls[-1].request.content.decode())
    assert pilot_payload["name"] == f"{experiment['name']} - Pilot"

    _mock_publish_study(study_id="PILOT_STUDY")
    publish_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/publish"
    )
    assert publish_resp.status_code == 200

    _mock_close_study(study_id="PILOT_STUDY")
    close_resp = client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/close")
    assert close_resp.status_code == 200

    round_route = _mock_create_study(study_id="ROUND_1_STUDY")
    round_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 4},
    )
    assert round_resp.status_code == 200, round_resp.text
    assert round_route.called
    round_payload = json.loads(round_route.calls[-1].request.content.decode())
    assert round_payload["name"] == f"{experiment['name']} - Round 1"


@respx.mock
def test_prolific_create_failure_returns_502(client: TestClient, enable_prolific):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    _mock_create_study(status=500)

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )

    assert resp.status_code == 502

    # Experiment remains, but no rounds were created and no study is linked.
    experiments = client.get("/api/admin/experiments").json()
    stored = next(item for item in experiments if item["id"] == experiment["id"])
    assert stored["prolific_completion_url"] is None
    rounds = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/rounds").json()
    assert rounds == []


@respx.mock
def test_prolific_create_includes_project_when_set(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_ABC")
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    assert sent["project"] == "PROJ_ABC"


@respx.mock
def test_prolific_create_omits_project_when_unset(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    # When project_id is empty, the field must be absent from the payload —
    # sending an empty string would 400 from Prolific.
    monkeypatch.setattr(get_settings().prolific, "project_id", "")
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    assert "project" not in sent


@respx.mock
def test_prolific_create_failure_propagates_message(client: TestClient, enable_prolific):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    # Pilot launch now ensures the experiment's own participant group before
    # creating the study — mock the group create so the study POST is what
    # actually returns the 400 we want to test.
    _install_default_participant_group_mock()
    respx.post(f"{PROLIFIC_BASE}/studies/").mock(
        return_value=Response(
            400,
            json={"error": {"detail": "Reward must be at least 100"}},
        )
    )

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert resp.status_code == 502
    assert "Reward must be at least 100" in resp.json()["detail"]


@respx.mock
def test_prolific_second_pilot_is_rejected(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "A pilot study has already been run for this experiment"


@respx.mock
def test_prolific_pilot_commit_conflict_deletes_orphaned_study(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    _patch_commit_to_fail_for_round(monkeypatch, round_number=0)
    create_route = _mock_create_study(study_id="PILOT_ORPHAN")
    delete_route = _mock_delete_study(study_id="PILOT_ORPHAN")

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "A pilot study has already been run for this experiment"
    assert create_route.called
    assert delete_route.called

    rounds = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/rounds").json()
    assert rounds == []


@respx.mock
def test_prolific_recommendation_returns_zeros_before_ratings(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)

    resp = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/recommend")

    assert resp.status_code == 200
    assert resp.json() == {
        "avg_time_per_question_seconds": 0.0,
        "remaining_rating_actions": 0,
        "total_hours_remaining": 0.0,
        "recommended_places": 0,
        "is_complete": False,
    }


@respx.mock
def test_prolific_recommendation_updates_after_pilot_rating(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_PILOT_RATER")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    started_at = datetime.now(UTC) - timedelta(seconds=45)
    submit_resp = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": started_at.isoformat(),
        },
    )
    assert submit_resp.status_code == 200

    resp = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/recommend")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["avg_time_per_question_seconds"] > 0
    assert payload["remaining_rating_actions"] == 3
    assert payload["total_hours_remaining"] > 0
    # 2 questions at target 2 with 1 rating collected: the unrated question
    # still needs 2 ratings from distinct raters, so 2 places is the floor
    # regardless of how fast raters are.
    assert payload["recommended_places"] == 2
    assert payload["is_complete"] is False


@respx.mock
def test_prolific_recommendation_honors_include_preview(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    _upload_questions(client, experiment["id"])

    response = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment["id"],
            "PROLIFIC_PID": "PID_PREVIEW",
            "STUDY_ID": "STUDY_PREVIEW",
            "SESSION_ID": "SESSION_PREVIEW",
            "preview": "true",
        },
    )
    assert response.status_code == 200
    session_payload = response.json()

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    submit_resp = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        },
    )
    assert submit_resp.status_code == 200

    default_resp = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/recommend")
    preview_resp = client.get(
        f"/api/admin/experiments/{experiment['id']}/prolific/recommend?include_preview=true"
    )

    assert default_resp.status_code == 200
    assert default_resp.json()["avg_time_per_question_seconds"] == 0.0
    assert preview_resp.status_code == 200
    assert preview_resp.json()["avg_time_per_question_seconds"] > 0


@respx.mock
def test_prolific_round_requires_pilot(client: TestClient, enable_prolific):
    experiment = _create_experiment(client)

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 4},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Run a pilot study first before launching a main round"


@respx.mock
def test_prolific_round_creation_requires_closing_previous_round(
    client: TestClient,
    enable_prolific,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    before_publish = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )
    assert before_publish.status_code == 400
    assert (
        before_publish.json()["detail"] == "Close the previous round before launching a new round"
    )

    _mock_publish_study()
    publish_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_resp.status_code == 200

    while_active = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )
    assert while_active.status_code == 400
    assert while_active.json()["detail"] == "Close the previous round before launching a new round"

    _mock_close_study()
    close_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close")
    assert close_resp.status_code == 200
    assert close_resp.json()["status"] == "AWAITING_REVIEW"

    _mock_create_study(study_id="ROUND_STUDY")
    first_round = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )
    assert first_round.status_code == 200, first_round.text
    assert first_round.json()["round_number"] == 1


@respx.mock
def test_prolific_round_commit_conflict_deletes_orphaned_study(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish").status_code
        == 200
    )
    _mock_close_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close").status_code
        == 200
    )

    _patch_commit_to_fail_for_round(monkeypatch, round_number=1)
    create_route = _mock_create_study(study_id="ROUND_ORPHAN")
    delete_route = _mock_delete_study(study_id="ROUND_ORPHAN")

    resp = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "A round with this number already exists for this experiment"
    assert create_route.called
    assert delete_route.called

    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert [round_["round_number"] for round_ in rounds] == [0]


@respx.mock
def test_prolific_round_commit_conflict_preserves_409_when_cleanup_fails(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish").status_code
        == 200
    )
    _mock_close_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close").status_code
        == 200
    )

    _patch_commit_to_fail_for_round(monkeypatch, round_number=1)
    _mock_create_study(study_id="ROUND_ORPHAN_DELETE_FAIL")
    delete_route = _mock_delete_study(study_id="ROUND_ORPHAN_DELETE_FAIL", status=500)

    resp = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "A round with this number already exists for this experiment"
    assert delete_route.called
    assert "Failed to clean up orphaned Prolific study after local DB failure" in caplog.text


@respx.mock
def test_prolific_round_history_and_completion_url_progression(
    client: TestClient,
    enable_prolific,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]
    initial_experiment = next(
        item for item in client.get("/api/admin/experiments").json() if item["id"] == experiment_id
    )
    initial_completion_url = initial_experiment["prolific_completion_url"]

    _mock_publish_study()
    publish_pilot = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_pilot.status_code == 200

    _mock_close_study()
    close_pilot = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close")
    assert close_pilot.status_code == 200

    _mock_create_study(study_id="ROUND_STUDY_1")
    round_one = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )
    assert round_one.status_code == 200

    _mock_publish_study(study_id="ROUND_STUDY_1")
    publish_round_one = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/2/publish"
    )
    assert publish_round_one.status_code == 200

    _mock_close_study(study_id="ROUND_STUDY_1", closed_status="COMPLETED")
    close_round_one = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/2/close")
    assert close_round_one.status_code == 200

    _mock_create_study(study_id="ROUND_STUDY_2")
    round_two = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 2},
    )
    assert round_two.status_code == 200

    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert [round_["round_number"] for round_ in rounds] == [0, 1, 2]
    assert [round_["places_requested"] for round_ in rounds] == [5, 4, 2]

    stored = next(
        item for item in client.get("/api/admin/experiments").json() if item["id"] == experiment_id
    )
    assert stored["prolific_completion_url"] == initial_completion_url


@respx.mock
def test_prolific_round_publish_updates_status(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    route = _mock_publish_study()
    resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")

    assert resp.status_code == 200
    assert route.called
    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert rounds[0]["prolific_study_status"] == "ACTIVE"


@respx.mock
def test_prolific_round_edit_updates_db_and_calls_prolific(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    route = _mock_update_study()
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={"description": "Updated description", "reward": 1500, "places": 7},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "Updated description"
    assert body["reward"] == 1500
    assert body["places_requested"] == 7
    assert route.called

    sent = json.loads(route.calls[0].request.content)
    # Description is converted to Prolific's HTML subset on the wire; the raw
    # markdown is what we store back in the DB and return in the response.
    assert sent == {
        "description": "<p>Updated description</p>",
        "reward": 1500,
        "total_available_places": 7,
    }


@respx.mock
def test_prolific_round_edit_updates_study_label_and_screeners(
    client: TestClient,
    enable_prolific,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    route = _mock_update_study()
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={"study_label": "survey", "screeners": ["fact_checkers"]},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["study_label"] == "survey"
    assert body["screeners"] == ["fact_checkers"]
    assert route.called

    sent = json.loads(route.calls[0].request.content)
    assert sent["study_labels"] == ["survey"]
    # The filter list always carries the experiment's own participant group so
    # raters from one round can't take another round of the same experiment.
    assert sent["filters"] == [
        {"filter_id": "fact-checkers", "selected_values": ["0"]},
        {
            "filter_id": "participant_group_blocklist",
            "selected_values": [_expected_group_id(experiment)],
        },
    ]

    # Confirm the change persisted and is reflected on round list.
    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert rounds[0]["study_label"] == "survey"
    assert rounds[0]["screeners"] == ["fact_checkers"]


@respx.mock
def test_prolific_round_edit_can_clear_all_screeners(
    client: TestClient,
    enable_prolific,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    route = _mock_update_study()
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={"screeners": []},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["screeners"] == []
    sent = json.loads(route.calls[0].request.content)
    # Clearing screeners leaves the own-group blocklist filter in place — the
    # own-group filter is independent of screener config.
    assert sent["filters"] == [
        {
            "filter_id": "participant_group_blocklist",
            "selected_values": [_expected_group_id(experiment)],
        },
    ]


@respx.mock
def test_prolific_round_edit_rejects_when_published(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    publish_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_resp.status_code == 200

    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={"description": "Cannot edit"},
    )

    assert resp.status_code == 400
    assert "unpublished" in resp.json()["detail"].lower()


@respx.mock
def test_prolific_round_edit_rejects_empty_payload(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={},
    )

    assert resp.status_code == 400


@respx.mock
def test_prolific_round_edit_returns_404_for_missing_round(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/9999",
        json={"description": "x"},
    )

    assert resp.status_code == 404


@respx.mock
def test_prolific_round_edit_returns_502_when_prolific_fails(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_update_study(status=500)
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={"description": "x"},
    )

    assert resp.status_code == 502


@respx.mock
def test_prolific_round_list_refreshes_transient_status_from_prolific(
    client: TestClient,
    enable_prolific,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    respx.post(f"{PROLIFIC_BASE}/studies/{PROLIFIC_STUDY_ID}/transition/").mock(
        return_value=Response(200, json={"id": PROLIFIC_STUDY_ID, "status": "PUBLISHING"})
    )
    publish_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_resp.status_code == 200
    assert publish_resp.json()["status"] == "PUBLISHING"

    route = _mock_get_study(study_status="ACTIVE")
    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()

    assert route.called
    assert rounds[0]["prolific_study_status"] == "ACTIVE"


@respx.mock
def test_prolific_round_sync_captures_total_cost_into_list_spend(
    client: TestClient,
    enable_prolific,
):
    experiment, pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    respx.post(f"{PROLIFIC_BASE}/studies/{PROLIFIC_STUDY_ID}/transition/").mock(
        return_value=Response(200, json={"id": PROLIFIC_STUDY_ID, "status": "PUBLISHING"})
    )
    assert (
        client.post(
            f"/api/admin/experiments/{experiment_id}/prolific/rounds/{pilot['id']}/publish"
        ).status_code
        == 200
    )

    # Listing rounds triggers the Prolific status sync, which also stores the
    # study's total_cost on the round; the experiment list then sums it as spend.
    _mock_get_study(study_status="ACTIVE", total_cost=1860)
    client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds")

    item = next(i for i in client.get("/api/admin/experiments").json() if i["id"] == experiment_id)
    assert item["spend_minor_units"] == 1860


@respx.mock
def test_sync_spend_hydrates_terminal_rounds_and_sums(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    """sync-spend refreshes every round's cost — including already-closed rounds
    that the status sync skips — sums them, and persists so the list reflects it."""
    experiment = _create_experiment(client)
    experiment_id = experiment["id"]
    # Two closed rounds with no cost captured yet.
    _insert_round(sync_engine, experiment_id=experiment_id, round_number=0, status="COMPLETED")
    _insert_round(sync_engine, experiment_id=experiment_id, round_number=1, status="COMPLETED")
    _mock_get_study(study_id=f"STUDY_{experiment_id}_0", study_status="COMPLETED", total_cost=1500)
    _mock_get_study(study_id=f"STUDY_{experiment_id}_1", study_status="COMPLETED", total_cost=900)

    resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/sync-spend")
    assert resp.status_code == 200, resp.text
    assert resp.json()["spend_minor_units"] == 2400

    item = next(i for i in client.get("/api/admin/experiments").json() if i["id"] == experiment_id)
    assert item["spend_minor_units"] == 2400


@respx.mock
def test_sync_spend_keeps_cached_cost_when_study_fetch_fails(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    """A round whose Prolific study can't be fetched keeps its last-known cost."""
    experiment = _create_experiment(client)
    experiment_id = experiment["id"]
    _insert_round(
        sync_engine,
        experiment_id=experiment_id,
        round_number=0,
        status="COMPLETED",
        total_cost=777,
    )
    _mock_get_study(study_id=f"STUDY_{experiment_id}_0", status=404)

    resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/sync-spend")
    assert resp.status_code == 200, resp.text
    assert resp.json()["spend_minor_units"] == 777


@respx.mock
def test_prolific_round_close_updates_status(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    publish_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_resp.status_code == 200

    route = _mock_close_study(closed_status="AWAITING_REVIEW")
    close_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close")

    assert close_resp.status_code == 200
    assert close_resp.json()["status"] == "AWAITING_REVIEW"
    assert route.called

    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert rounds[0]["prolific_study_status"] == "AWAITING_REVIEW"


@respx.mock
def test_prolific_round_close_handles_space_separated_status(client: TestClient, enable_prolific):
    # Prolific's STOP transition returns the status space-separated
    # ("AWAITING REVIEW"), which must not 500 the close endpoint.
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    publish_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_resp.status_code == 200

    route = _mock_close_study(closed_status="AWAITING REVIEW")
    close_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close")

    assert close_resp.status_code == 200, close_resp.text
    assert close_resp.json()["status"] == "AWAITING_REVIEW"
    assert route.called

    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert rounds[0]["prolific_study_status"] == "AWAITING_REVIEW"


@respx.mock
def test_prolific_delete_calls_prolific_api_for_all_rounds(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish").status_code
        == 200
    )
    _mock_close_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close").status_code
        == 200
    )
    _mock_create_study(study_id="ROUND_STUDY_DELETE")
    assert (
        client.post(
            f"/api/admin/experiments/{experiment_id}/prolific/rounds",
            json={"places": 4},
        ).status_code
        == 200
    )

    pilot_delete = _mock_delete_study(study_id=PROLIFIC_STUDY_ID)
    round_delete = _mock_delete_study(study_id="ROUND_STUDY_DELETE")

    resp = client.delete(f"/api/admin/experiments/{experiment_id}")

    assert resp.status_code == 200
    assert pilot_delete.called
    assert round_delete.called

    experiments = client.get("/api/admin/experiments").json()
    assert all(e["id"] != experiment_id for e in experiments)


@respx.mock
def test_prolific_delete_succeeds_when_api_fails(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_delete_study(status=500)

    resp = client.delete(f"/api/admin/experiments/{experiment_id}")

    # Local delete succeeds even when Prolific API fails.
    assert resp.status_code == 200


@respx.mock
def test_prolific_delete_handles_404(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_delete_study(status=404)

    resp = client.delete(f"/api/admin/experiments/{experiment_id}")

    assert resp.status_code == 200


def test_platform_status_reflects_prolific_enabled(client: TestClient, enable_prolific):
    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    assert resp.json()["prolific_enabled"] is True


def test_platform_status_disabled_by_default(client: TestClient):
    settings = get_settings()
    original = settings.prolific.api_token
    settings.prolific.api_token = ""
    try:
        resp = client.get("/api/admin/platform-status")
        assert resp.status_code == 200
        assert resp.json()["prolific_enabled"] is False
    finally:
        settings.prolific.api_token = original


@respx.mock
def test_platform_status_returns_workspace_currency(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_ABC")

    _mock_project(project_id="PROJ_ABC", workspace_id="WS_ABC")
    _mock_workspace_balance(workspace_id="WS_ABC", currency_code="USD")

    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency_code"] == "USD"
    assert body["currency_symbol"] == "$"


def test_platform_status_currency_null_when_project_id_unset(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "")

    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency_code"] is None
    assert body["currency_symbol"] is None


@respx.mock
def test_platform_status_currency_null_when_project_lookup_fails(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_NOT_FOUND")

    _mock_project(project_id="PROJ_NOT_FOUND", status=404)
    balance_route = _mock_workspace_balance(workspace_id="WS_ABC")

    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency_code"] is None
    assert body["currency_symbol"] is None
    # Project lookup fails before we learn the workspace, so balance is never queried.
    assert not balance_route.called


@respx.mock
def test_platform_status_currency_null_when_balance_fails(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_ABC")

    _mock_project(project_id="PROJ_ABC", workspace_id="WS_ABC")
    _mock_workspace_balance(workspace_id="WS_ABC", status=500)

    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency_code"] is None
    assert body["currency_symbol"] is None


@respx.mock
def test_platform_status_currency_cached_across_calls(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_ABC")

    project_route = _mock_project(project_id="PROJ_ABC", workspace_id="WS_ABC")
    balance_route = _mock_workspace_balance(workspace_id="WS_ABC", currency_code="GBP")

    resp1 = client.get("/api/admin/platform-status")
    resp2 = client.get("/api/admin/platform-status")

    assert resp1.json()["currency_code"] == "GBP"
    assert resp1.json()["currency_symbol"] == "£"
    assert resp2.json()["currency_code"] == "GBP"
    assert project_route.call_count == 1
    assert balance_route.call_count == 1


# ── parent_question_id (sub-questions) ────────────────────────────────────────

PARENT_TEXT = "Customer review: arrived late but exceeded expectations."


def _upload_parent_and_children(client: TestClient, experiment_id: int) -> None:
    csv_data = (
        "question_id,question_text,gt_answer,options,question_type,parent_question_id\n"
        f'parent1,"{PARENT_TEXT}",,,,\n'
        "sub_satisfied,Does the review express satisfaction?,Yes,Yes|No,MC,parent1\n"
        "sub_problem,Does the review describe a delivery problem?,Yes,Yes|No,MC,parent1\n"
    )
    response = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("questions.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 200, response.text


def test_upload_with_parent_question_id_attaches_context_to_children(client: TestClient):
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])

    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_PARENT_CTX")
    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    # Both eligible questions are children; either should carry the parent text.
    assert question["question_id"] in {"sub_satisfied", "sub_problem"}
    assert question["parent_question_text"] == PARENT_TEXT


def test_upload_without_parent_question_id_returns_null_context(client: TestClient):
    """Backwards compat: standalone questions still have parent_question_text == None."""
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])  # legacy CSV without the column

    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_LEGACY")
    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    assert question["parent_question_text"] is None


def test_upload_rejects_self_referential_parent(client: TestClient):
    experiment = _create_experiment(client)
    csv_data = "question_id,question_text,parent_question_id\nloop,Self-referential question,loop\n"
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("self_ref.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 400
    assert "cannot reference itself" in response.json()["detail"]


def test_upload_rejects_unresolvable_parent_reference(client: TestClient):
    experiment = _create_experiment(client)
    csv_data = (
        "question_id,question_text,parent_question_id\n"
        "orphan,Child without a parent,does_not_exist\n"
    )
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("orphan.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "does_not_exist" in detail
    assert "orphan" in detail


def test_next_question_never_returns_parent_rows(client: TestClient):
    """Parents are header rows; the rater should only ever see children."""
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_NO_PARENT")
    headers = _rater_headers(session_payload)

    seen_question_ids: set[str] = set()
    for _ in range(2):
        resp = client.get("/api/raters/next-question", headers=headers)
        assert resp.status_code == 200
        question = resp.json()
        seen_question_ids.add(question["question_id"])
        client.post(
            "/api/raters/submit",
            headers=headers,
            json={
                "question_id": question["id"],
                "answer": "Yes",
                "confidence": 4,
                "time_started": datetime.now(UTC).isoformat(),
            },
        )

    assert seen_question_ids == {"sub_satisfied", "sub_problem"}
    assert "parent1" not in seen_question_ids


def test_next_question_groups_siblings_together(client: TestClient):
    """Once a rater starts a parent group, remaining picks should be siblings
    of that group before any question from a different group is served."""
    experiment = _create_experiment(client)
    csv_data = (
        "question_id,question_text,gt_answer,options,question_type,parent_question_id\n"
        'pA,"Parent A context",,,,\n'
        "a1,Child A1?,Yes,Yes|No,MC,pA\n"
        "a2,Child A2?,Yes,Yes|No,MC,pA\n"
        'pB,"Parent B context",,,,\n'
        "b1,Child B1?,Yes,Yes|No,MC,pB\n"
        "b2,Child B2?,Yes,Yes|No,MC,pB\n"
    )
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("two_groups.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 200, response.text

    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_GROUPING")
    headers = _rater_headers(session_payload)

    sibling_of = {"a1": "a2", "a2": "a1", "b1": "b2", "b2": "b1"}
    served_order: list[str] = []
    for _ in range(4):
        question = client.get("/api/raters/next-question", headers=headers).json()
        served_order.append(question["question_id"])
        submit = client.post(
            "/api/raters/submit",
            headers=headers,
            json={
                "question_id": question["id"],
                "answer": "Yes",
                "confidence": 4,
                "time_started": datetime.now(UTC).isoformat(),
            },
        )
        assert submit.status_code == 200

    # First two picks (whichever group came first) must be siblings; same for last two.
    assert served_order[1] == sibling_of[served_order[0]], served_order
    assert served_order[3] == sibling_of[served_order[2]], served_order
    assert set(served_order) == {"a1", "a2", "b1", "b2"}


def test_stats_total_questions_excludes_parent_rows(client: TestClient):
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])

    stats = client.get(f"/api/admin/experiments/{experiment['id']}/stats").json()
    # parent1 + 2 children uploaded, but parent1 is not ratable.
    assert stats["total_questions"] == 2


def test_list_experiments_question_count_excludes_parent_rows(client: TestClient):
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])

    items = client.get("/api/admin/experiments").json()
    matching = next(item for item in items if item["id"] == experiment["id"])
    assert matching["question_count"] == 2


def test_recommendation_remaining_actions_excludes_parent_rows(client: TestClient):
    """Bug guard: ghost parent rows used to inflate recommended_places."""
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])

    # num_ratings_per_question defaults to 2 in _create_experiment, 2 children → 4 actions total.
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_REC")
    headers = _rater_headers(session_payload)
    question = client.get("/api/raters/next-question", headers=headers).json()
    submit = client.post(
        "/api/raters/submit",
        headers=headers,
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        },
    )
    assert submit.status_code == 200

    resp = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/recommend")
    assert resp.status_code == 200
    payload = resp.json()
    # 2 children × 2 ratings - 1 submitted = 3. (Pre-fix this was 5: 3 questions × 2 - 1.)
    assert payload["remaining_rating_actions"] == 3


@respx.mock
def test_prolific_create_converts_description_markdown_to_html(
    client: TestClient,
    enable_prolific,
):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json={
            **_pilot_payload(),
            "description": "Read the article.\n\n- Be fair\n- Be quick",
        },
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    assert (
        sent["description"] == "<p>Read the article.</p><ul><li>Be fair</li><li>Be quick</li></ul>"
    )


@respx.mock
def test_prolific_create_sends_internal_name_when_set(
    client: TestClient,
    enable_prolific,
):
    create_resp = client.post(
        "/api/admin/experiments",
        json={
            "name": _unique_name("public-exp"),
            "internal_name": "Internal Q2 Eval",
            "num_ratings_per_question": 2,
            "prolific_completion_url": None,
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()
    assert experiment["internal_name"] == "Internal Q2 Eval"

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    assert sent["internal_name"] == "Internal Q2 Eval - Pilot"


@respx.mock
def test_prolific_create_omits_internal_name_when_unset(
    client: TestClient,
    enable_prolific,
):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()
    assert experiment["internal_name"] is None

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    assert "internal_name" not in sent
    # `annotation` is the schema default so we still emit study_labels.
    assert sent["study_labels"] == ["annotation"]


@respx.mock
def test_prolific_create_sends_chosen_study_label(
    client: TestClient,
    enable_prolific,
):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json={**_pilot_payload(), "study_label": "survey"},
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    assert sent["study_labels"] == ["survey"]


@respx.mock
def test_prolific_round_inherits_pilot_study_label(
    client: TestClient,
    enable_prolific,
):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200
    experiment = create_resp.json()

    _mock_create_study(study_id="PILOT_S")
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json={**_pilot_payload(), "study_label": "decision_making_task"},
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    _mock_publish_study(study_id="PILOT_S")
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/publish")
    _mock_close_study(study_id="PILOT_S")
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/close")

    round_route = _mock_create_study(study_id="R1_S")
    round_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 3},
    )
    assert round_resp.status_code == 200, round_resp.text
    sent = json.loads(round_route.calls[-1].request.content.decode())
    assert sent["study_labels"] == ["decision_making_task"]


@respx.mock
def test_prolific_create_sends_default_screeners(
    client: TestClient,
    enable_prolific,
):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    filters = sent["filters"]
    assert {"filter_id": "ai-taskers", "selected_values": ["0"]} in filters
    assert {"filter_id": "fact-checkers", "selected_values": ["0"]} in filters
    assert {
        "filter_id": "approval_rate",
        "selected_range": {"lower": 80, "upper": 100},
    } in filters


@respx.mock
def test_prolific_create_sends_only_own_group_when_screeners_empty(
    client: TestClient,
    enable_prolific,
):
    """With no screeners and no explicit exclusions, the round still ships a
    filter list containing the experiment's own participant group."""
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json={**_pilot_payload(), "screeners": []},
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    assert sent["filters"] == [
        {
            "filter_id": "participant_group_blocklist",
            "selected_values": [_expected_group_id(experiment)],
        },
    ]


@respx.mock
def test_prolific_round_inherits_pilot_screeners(
    client: TestClient,
    enable_prolific,
):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200
    experiment = create_resp.json()

    _mock_create_study(study_id="PILOT_SCR")
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json={**_pilot_payload(), "screeners": ["ai_taskers"]},
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    _mock_publish_study(study_id="PILOT_SCR")
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/publish")
    _mock_close_study(study_id="PILOT_SCR")
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/close")

    round_route = _mock_create_study(study_id="R1_SCR")
    round_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 3},
    )
    assert round_resp.status_code == 200, round_resp.text
    sent = json.loads(round_route.calls[-1].request.content.decode())
    assert sent["filters"] == [
        {"filter_id": "ai-taskers", "selected_values": ["0"]},
        {
            "filter_id": "participant_group_blocklist",
            "selected_values": [_expected_group_id(experiment)],
        },
    ]


def _expected_group_id(experiment: dict) -> str:
    """Reproduces the ID that `_install_default_participant_group_mock`
    returns for `experiment` — the participant group name as computed by
    services.participant_groups.participant_group_name, including the
    PROLIFIC__ENV_LABEL prefix that dev environments set."""
    prefix = get_settings().prolific.env_label.strip()
    parts = [prefix] if prefix else []
    parts.extend(["exp", str(experiment["id"]), _slugify_for_prolific(experiment["name"])])
    return "-".join(parts)


def _blocklist_values(filters: list[dict]) -> list[str]:
    for entry in filters:
        if entry.get("filter_id") == "participant_group_blocklist":
            return entry.get("selected_values", [])
    return []


@respx.mock
def test_prolific_pilot_with_exclusion_creates_group_and_sends_blocklist(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    """Launching a pilot with `excluded_experiment_ids` should create groups
    for both the current experiment and the excluded one, and include both
    in the `participant_group_blocklist` filter on the create-study payload."""
    excluded_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert excluded_resp.status_code == 200, excluded_resp.text
    excluded = excluded_resp.json()
    _mark_experiment_status(sync_engine, excluded["id"], "FINISHED")

    new_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert new_resp.status_code == 200, new_resp.text
    new = new_resp.json()

    study_route = _mock_create_study(study_id="PILOT_EXCL")
    pilot_resp = client.post(
        f"/api/admin/experiments/{new['id']}/prolific/pilot",
        json={**_pilot_payload(), "excluded_experiment_ids": [excluded["id"]]},
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    sent = json.loads(study_route.calls[-1].request.content.decode())
    # Blocklist should contain the current experiment's own group (so raters
    # of one round can't take another round of the same experiment) plus the
    # explicitly excluded experiment's group.
    assert set(_blocklist_values(sent["filters"])) == {
        _expected_group_id(new),
        _expected_group_id(excluded),
    }
    assert pilot_resp.json()["excluded_experiment_ids"] == [excluded["id"]]


@respx.mock
def test_prolific_pilot_reuses_existing_group_no_duplicate_creation(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    """Second pilot referencing the same excluded experiment should not create
    a new participant group for that experiment — its group ID is persisted on
    first use and reused thereafter."""
    excluded_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    excluded = excluded_resp.json()
    _mark_experiment_status(sync_engine, excluded["id"], "FINISHED")

    exp1_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    exp1 = exp1_resp.json()
    exp2_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    exp2 = exp2_resp.json()

    # Record every requested group name so we can distinguish per-experiment
    # creates from reuses. Register the spy AFTER `_mock_create_study` (which
    # installs a default group mock as a companion) so the spy takes precedence
    # for the subsequent calls — respx resolves later-registered routes first.
    created_names: list[str] = []

    def _spy(request):
        body = json.loads(request.content.decode())
        name = body.get("name", "test-group")
        created_names.append(name)
        return Response(200, json={"id": name, "name": name, "project_id": "p"})

    _mock_create_study(study_id="PILOT_A")
    respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(side_effect=_spy)
    resp1 = client.post(
        f"/api/admin/experiments/{exp1['id']}/prolific/pilot",
        json={**_pilot_payload(), "excluded_experiment_ids": [excluded["id"]]},
    )
    assert resp1.status_code == 200, resp1.text

    _mock_create_study(study_id="PILOT_B")
    respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(side_effect=_spy)
    resp2 = client.post(
        f"/api/admin/experiments/{exp2['id']}/prolific/pilot",
        json={**_pilot_payload(), "excluded_experiment_ids": [excluded["id"]]},
    )
    assert resp2.status_code == 200, resp2.text

    # Expected group creations across both pilots: exp1's own, excluded's own
    # (created on first use as an exclusion source), exp2's own. The excluded
    # experiment's group must not be re-created for pilot 2.
    assert created_names == [
        _expected_group_id(exp1),
        _expected_group_id(excluded),
        _expected_group_id(exp2),
    ]


@respx.mock
def test_prolific_round_edit_adds_exclusion(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    """Adding exclusions to an unpublished round should PATCH the study with
    combined screener + participant-group filters, including the current
    experiment's own group."""
    excluded_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    excluded = excluded_resp.json()
    _mark_experiment_status(sync_engine, excluded["id"], "FINISHED")

    experiment, _pilot = _create_prolific_experiment(client)

    update_route = _mock_update_study()
    resp = client.patch(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1",
        json={"excluded_experiment_ids": [excluded["id"]]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["excluded_experiment_ids"] == [excluded["id"]]

    sent = json.loads(update_route.calls[0].request.content.decode())
    assert set(_blocklist_values(sent["filters"])) == {
        _expected_group_id(experiment),
        _expected_group_id(excluded),
    }


@respx.mock
def test_prolific_round_edit_drops_deleted_exclusion_target(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    """A round that excludes an experiment which is later deleted must stay
    editable. The deleted target has no group to block and can't be unselected
    in the picker, so the edit should drop it silently (not 400), send a
    blocklist with only the survivors, and self-heal the stored list."""
    excluded_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    excluded = excluded_resp.json()
    _mark_experiment_status(sync_engine, excluded["id"], "FINISHED")

    experiment, _pilot = _create_prolific_experiment(client)

    # Attach the exclusion while the target still exists.
    _mock_update_study()
    resp = client.patch(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1",
        json={"excluded_experiment_ids": [excluded["id"]]},
    )
    assert resp.status_code == 200, resp.text

    # The target experiment is hard-deleted; its ID lingers in the round's
    # stored exclusion list.
    delete_resp = client.delete(f"/api/admin/experiments/{excluded['id']}")
    assert delete_resp.status_code == 200, delete_resp.text

    # Re-submitting the form (still holding the ghost ID) must succeed. This is
    # the path that previously raised "Excluded experiment N does not exist."
    update_route = _mock_update_study()
    resp = client.patch(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1",
        json={"excluded_experiment_ids": [excluded["id"]]},
    )
    assert resp.status_code == 200, resp.text
    # Stored list self-heals: the deleted target is gone.
    assert resp.json()["excluded_experiment_ids"] == []

    # Blocklist sent to Prolific carries only the experiment's own group.
    sent = json.loads(update_route.calls[-1].request.content.decode())
    assert _blocklist_values(sent["filters"]) == [_expected_group_id(experiment)]


@respx.mock
def test_prolific_main_round_inherits_pilot_exclusions(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    excluded_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    excluded = excluded_resp.json()
    _mark_experiment_status(sync_engine, excluded["id"], "FINISHED")

    new_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    new = new_resp.json()

    _mock_create_study(study_id="PILOT_INH")
    pilot_resp = client.post(
        f"/api/admin/experiments/{new['id']}/prolific/pilot",
        json={**_pilot_payload(), "excluded_experiment_ids": [excluded["id"]]},
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    _mock_publish_study(study_id="PILOT_INH")
    client.post(f"/api/admin/experiments/{new['id']}/prolific/rounds/1/publish")
    _mock_close_study(study_id="PILOT_INH")
    client.post(f"/api/admin/experiments/{new['id']}/prolific/rounds/1/close")

    round_route = _mock_create_study(study_id="R1_INH")
    round_resp = client.post(
        f"/api/admin/experiments/{new['id']}/prolific/rounds",
        json={"places": 3},
    )
    assert round_resp.status_code == 200, round_resp.text
    sent = json.loads(round_route.calls[-1].request.content.decode())
    assert set(_blocklist_values(sent["filters"])) == {
        _expected_group_id(new),
        _expected_group_id(excluded),
    }
    assert round_resp.json()["excluded_experiment_ids"] == [excluded["id"]]


@respx.mock
def test_prolific_pilot_blocks_own_group_by_default(
    client: TestClient,
    enable_prolific,
):
    """Even without any explicit exclusions, every round launch should include
    the experiment's own group in the blocklist. Groups are dynamic, so this
    is what keeps raters from one round out of every other round of the same
    experiment."""
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    experiment = create_resp.json()

    study_route = _mock_create_study(study_id="PILOT_OWN")
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json={**_pilot_payload(), "screeners": []},
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    sent = json.loads(study_route.calls[-1].request.content.decode())
    assert _blocklist_values(sent["filters"]) == [_expected_group_id(experiment)]


@respx.mock
def test_prolific_pilot_dedupes_excluded_ids(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    """Duplicate IDs in `excluded_experiment_ids` should be collapsed at the
    API boundary — the study payload must not repeat a group in its blocklist,
    and the round response echoes the deduped list."""
    excluded_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    excluded = excluded_resp.json()
    _mark_experiment_status(sync_engine, excluded["id"], "FINISHED")

    new_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    new = new_resp.json()

    study_route = _mock_create_study(study_id="PILOT_DEDUP")
    pilot_resp = client.post(
        f"/api/admin/experiments/{new['id']}/prolific/pilot",
        json={
            **_pilot_payload(),
            "excluded_experiment_ids": [excluded["id"], excluded["id"]],
        },
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    assert pilot_resp.json()["excluded_experiment_ids"] == [excluded["id"]]
    sent = json.loads(study_route.calls[-1].request.content.decode())
    values = _blocklist_values(sent["filters"])
    assert len(values) == len(set(values))


@respx.mock
def test_rater_start_session_adds_participant_to_group(
    client: TestClient,
    enable_prolific,
):
    """A non-preview rater starting a session should be POSTed to the
    experiment's participant group so later experiments can blocklist them."""
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    experiment = create_resp.json()
    _upload_questions(client, experiment["id"])

    _mock_create_study(study_id="PILOT_ADD")
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    expected_group = _expected_group_id(experiment)
    add_route = respx.post(
        f"{PROLIFIC_BASE}/participant-groups/{expected_group}/participants/"
    ).mock(return_value=Response(200, json={"participant_ids": ["PID_R1"]}))

    start_resp = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment["id"],
            "PROLIFIC_PID": "PID_R1",
            "STUDY_ID": "STUDY_ADD",
            "SESSION_ID": "SESSION_R1",
        },
    )
    assert start_resp.status_code == 200, start_resp.text
    assert add_route.called, "expected rater to be POSTed to the participant group"
    body = json.loads(add_route.calls[-1].request.content.decode())
    assert body == {"participant_ids": ["PID_R1"]}


@respx.mock
def test_rater_start_session_tolerates_prolific_add_failure(
    client: TestClient,
    enable_prolific,
):
    """Adding a rater to the participant group is best-effort — a failure from
    Prolific must never block rater entry, and the rater must still get a
    valid session token back."""
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    experiment = create_resp.json()
    _upload_questions(client, experiment["id"])

    _mock_create_study(study_id="PILOT_ADDFAIL")
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    expected_group = _expected_group_id(experiment)
    respx.post(f"{PROLIFIC_BASE}/participant-groups/{expected_group}/participants/").mock(
        return_value=Response(400, json={"error": {"error_code": 140003}}),
    )

    start_resp = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment["id"],
            "PROLIFIC_PID": "PID_R_BAD",
            "STUDY_ID": "STUDY_ADDFAIL",
            "SESSION_ID": "SESSION_R_BAD",
        },
    )
    assert start_resp.status_code == 200, start_resp.text
    assert start_resp.json().get("rater_session_token")


@respx.mock
def test_rater_start_session_skips_group_add_for_preview(
    client: TestClient,
    enable_prolific,
):
    """Preview raters aren't real Prolific participants — the group-add call
    would 400 with an unknown participant ID, so we skip it entirely."""
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    experiment = create_resp.json()
    _upload_questions(client, experiment["id"])

    _mock_create_study(study_id="PILOT_PREVIEW")
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    expected_group = _expected_group_id(experiment)
    add_route = respx.post(
        f"{PROLIFIC_BASE}/participant-groups/{expected_group}/participants/"
    ).mock(return_value=Response(200, json={"participant_ids": []}))

    start_resp = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment["id"],
            "PROLIFIC_PID": "PID_PREVIEW",
            "STUDY_ID": "STUDY_PREVIEW",
            "SESSION_ID": "SESSION_PREVIEW",
            "preview": "true",
        },
    )
    assert start_resp.status_code == 200, start_resp.text
    assert not add_route.called, "preview raters should not be added to the group"


# ── Experiment lifecycle state machine ───────────────────────────────────────
# DRAFT -> LAUNCH (auto, on first main round) -> FINISHED (manual). Delete
# stays legal in every state. Once past DRAFT, experiment-level config is
# locked; exclusion targets must be FINISHED (with grandfathering for IDs
# already on a round when its target's status changed).


def test_new_experiment_starts_in_draft(client: TestClient):
    experiment = _create_experiment(client)
    assert experiment["status"] == "DRAFT"


@respx.mock
def test_experiment_transitions_to_launch_on_first_publish(
    client: TestClient,
    enable_prolific,
):
    experiment, _pilot = _create_prolific_experiment(client)
    stored = next(
        item
        for item in client.get("/api/admin/experiments").json()
        if item["id"] == experiment["id"]
    )
    # Creating the pilot alone leaves the experiment in DRAFT — nothing is live
    # on Prolific until publish, so config stays editable.
    assert stored["status"] == "DRAFT"

    _mock_publish_study()
    publish_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/publish",
    )
    assert publish_resp.status_code == 200, publish_resp.text

    # Publishing the first round — even the pilot — flips DRAFT to LAUNCH:
    # participants may start rating any moment, so config freezes.
    stored = next(
        item
        for item in client.get("/api/admin/experiments").json()
        if item["id"] == experiment["id"]
    )
    assert stored["status"] == "LAUNCH"


@respx.mock
def test_experiment_launch_is_idempotent_on_subsequent_publishes(
    client: TestClient,
    enable_prolific,
):
    """Second and later publishes must not disturb an already-LAUNCH status."""
    experiment, _pilot = _create_prolific_experiment(client)
    _mock_publish_study()
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/publish")
    _mock_close_study()
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/close")

    _mock_create_study(study_id="R1")
    client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 3},
    )
    _mock_publish_study(study_id="R1")
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/2/publish")

    stored = next(
        item
        for item in client.get("/api/admin/experiments").json()
        if item["id"] == experiment["id"]
    )
    assert stored["status"] == "LAUNCH"


def test_update_experiment_locked_after_launch(client: TestClient, sync_engine):
    experiment = _create_experiment(client)
    _mark_experiment_status(sync_engine, experiment["id"], "LAUNCH")

    resp = client.patch(
        f"/api/admin/experiments/{experiment['id']}",
        json={"assistance_method": "top_n", "assistance_params": {"n": 4}},
    )
    assert resp.status_code == 400
    assert "assistance_method" in resp.json()["detail"]


def test_update_experiment_allows_unchanged_locked_fields(
    client: TestClient,
    sync_engine,
):
    """The frontend often re-sends unchanged locked fields alongside the one
    field it's editing. Only actual value changes should trip the lock."""
    experiment = _create_experiment(client)
    _mark_experiment_status(sync_engine, experiment["id"], "LAUNCH")

    resp = client.patch(
        f"/api/admin/experiments/{experiment['id']}",
        json={
            "assistance_method": experiment["assistance_method"],
            "assistance_params": experiment["assistance_params"],
            "internal_name": "renamed",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["internal_name"] == "renamed"


def test_upload_questions_locked_after_launch(client: TestClient, sync_engine):
    experiment = _create_experiment(client)
    _mark_experiment_status(sync_engine, experiment["id"], "LAUNCH")

    csv_data = "question_id,question_text\nq1,Is this ok?"
    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("questions.csv", csv_data, "text/csv")},
    )
    assert resp.status_code == 400
    assert "LAUNCH" in resp.json()["detail"]


def test_delete_allowed_in_every_state(client: TestClient, sync_engine):
    for status in ("DRAFT", "LAUNCH", "FINISHED"):
        experiment = _create_experiment(client)
        _mark_experiment_status(sync_engine, experiment["id"], status)
        resp = client.delete(f"/api/admin/experiments/{experiment['id']}")
        assert resp.status_code == 200, f"delete failed in {status}: {resp.text}"


@respx.mock
def test_finish_experiment_requires_all_rounds_terminal(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    experiment, pilot = _create_prolific_experiment(client)
    # LAUNCH the experiment by hand — we don't need a real round for the
    # finish-precondition check itself, just the state transition.
    _mark_experiment_status(sync_engine, experiment["id"], "LAUNCH")

    # Pilot is still UNPUBLISHED → non-terminal → cannot finish.
    resp = client.post(f"/api/admin/experiments/{experiment['id']}/finish")
    assert resp.status_code == 400
    assert "Non-terminal" in resp.json()["detail"]

    _mock_publish_study()
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{pilot['id']}/publish")
    _mock_close_study()
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{pilot['id']}/close")

    resp = client.post(f"/api/admin/experiments/{experiment['id']}/finish")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "FINISHED"


def test_finish_requires_launch_state(client: TestClient):
    experiment = _create_experiment(client)  # still DRAFT
    resp = client.post(f"/api/admin/experiments/{experiment['id']}/finish")
    assert resp.status_code == 400


def test_finish_is_terminal(client: TestClient, sync_engine):
    experiment = _create_experiment(client)
    _mark_experiment_status(sync_engine, experiment["id"], "FINISHED")
    resp = client.post(f"/api/admin/experiments/{experiment['id']}/finish")
    assert resp.status_code == 400
    assert "already finished" in resp.json()["detail"].lower()


@respx.mock
def test_pilot_exclusion_rejects_non_finished_target(
    client: TestClient,
    enable_prolific,
):
    """Fresh DRAFT target must be rejected as an exclusion source; only
    FINISHED experiments are valid pickers on new writes."""
    draft_target = client.post("/api/admin/experiments", json=_prolific_experiment_payload()).json()

    new = client.post("/api/admin/experiments", json=_prolific_experiment_payload()).json()
    _mock_create_study(study_id="PILOT_REJ")
    resp = client.post(
        f"/api/admin/experiments/{new['id']}/prolific/pilot",
        json={**_pilot_payload(), "excluded_experiment_ids": [draft_target["id"]]},
    )
    assert resp.status_code == 400
    assert "finished" in resp.json()["detail"].lower()


@respx.mock
def test_round_update_grandfathers_existing_exclusion(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    """A target that was FINISHED when added to a round stays valid even if
    its status later changes — grandfathering keeps unrelated edits from
    tripping on stale IDs."""
    target = client.post("/api/admin/experiments", json=_prolific_experiment_payload()).json()
    _mark_experiment_status(sync_engine, target["id"], "FINISHED")

    experiment, _pilot = _create_prolific_experiment(client)

    _mock_update_study()
    add_resp = client.patch(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1",
        json={"excluded_experiment_ids": [target["id"]]},
    )
    assert add_resp.status_code == 200, add_resp.text

    # Target regresses out of FINISHED — a re-write of the same list must
    # still succeed for the grandfathered ID.
    _mark_experiment_status(sync_engine, target["id"], "DRAFT")
    keep_resp = client.patch(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1",
        json={"excluded_experiment_ids": [target["id"]]},
    )
    assert keep_resp.status_code == 200, keep_resp.text


@respx.mock
def test_round_update_rejects_newly_added_non_finished_target(
    client: TestClient,
    enable_prolific,
):
    draft_target = client.post("/api/admin/experiments", json=_prolific_experiment_payload()).json()
    experiment, _pilot = _create_prolific_experiment(client)

    resp = client.patch(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1",
        json={"excluded_experiment_ids": [draft_target["id"]]},
    )
    assert resp.status_code == 400
    assert "finished" in resp.json()["detail"].lower()


@respx.mock
def test_cannot_launch_new_round_after_finished(
    client: TestClient,
    enable_prolific,
    sync_engine,
):
    experiment, _pilot = _create_prolific_experiment(client)
    _mark_experiment_status(sync_engine, experiment["id"], "FINISHED")

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 3},
    )
    assert resp.status_code == 400
    assert "finished" in resp.json()["detail"].lower()


@respx.mock
def test_finish_end_to_end_from_draft(
    client: TestClient,
    enable_prolific,
):
    """Full lifecycle without `_mark_experiment_status`: pilot → publish →
    close → main round (natural DRAFT to LAUNCH) → publish → close → finish.
    Guards the transition invariants that the shortcut bypasses."""
    experiment, pilot = _create_prolific_experiment(client)

    _mock_publish_study()
    publish_pilot = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{pilot['id']}/publish"
    )
    assert publish_pilot.status_code == 200, publish_pilot.text
    _mock_close_study()
    close_pilot = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{pilot['id']}/close"
    )
    assert close_pilot.status_code == 200, close_pilot.text

    _mock_create_study(study_id="MAIN")
    main_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 3},
    )
    assert main_resp.status_code == 200, main_resp.text
    main_round = main_resp.json()

    stored = next(
        item
        for item in client.get("/api/admin/experiments").json()
        if item["id"] == experiment["id"]
    )
    assert stored["status"] == "LAUNCH"

    # Main round still UNPUBLISHED — finish must reject.
    early_finish = client.post(f"/api/admin/experiments/{experiment['id']}/finish")
    assert early_finish.status_code == 400
    assert "Non-terminal" in early_finish.json()["detail"]

    _mock_publish_study(study_id="MAIN")
    client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{main_round['id']}/publish"
    )
    _mock_close_study(study_id="MAIN")
    client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{main_round['id']}/close"
    )

    finish_resp = client.post(f"/api/admin/experiments/{experiment['id']}/finish")
    assert finish_resp.status_code == 200, finish_resp.text
    assert finish_resp.json()["status"] == "FINISHED"


# ── Duplicate experiment ──────────────────────────────────────────────────────


def test_duplicate_copies_dataset_instructions_and_ratings_target(client: TestClient):
    base_name = _unique_name("dup-source")
    created = client.post(
        "/api/admin/experiments",
        json={
            "name": base_name,
            "internal_name": f"{base_name}-internal",
            "num_ratings_per_question": 4,
        },
    ).json()
    meta = {
        "description": "Pilot dataset on x.",
        "system_prompt": "You are an evaluator.",
        "human_prompt_prefix": "Consider the text below.",
        "human_prompt_suffix": "Pick the best option.",
        "prolific_pool": "uk_representative_sample",
    }
    client.post(
        f"/api/admin/experiments/{created['id']}/upload",
        files={"file": ("with_meta.csv", _csv_with_meta(meta), "text/csv")},
    )

    response = client.post(f"/api/admin/experiments/{created['id']}/duplicate")
    assert response.status_code == 200, response.text
    copy = response.json()

    assert copy["id"] != created["id"]
    assert copy["name"] == f"{base_name} COPY"
    assert copy["internal_name"] == f"{base_name}-internal COPY"
    assert copy["num_ratings_per_question"] == 4
    for key, value in meta.items():
        assert copy[key] == value
    assert copy["question_count"] == 2
    assert copy["rating_count"] == 0

    # Everything else resets to defaults.
    assert copy["status"] == "DRAFT"
    assert copy["assistance_method"] == "none"
    assert copy["assistance_params"] is None
    assert copy["prolific_completion_url"] is None

    # Upload provenance travels with the dataset.
    uploads = client.get(f"/api/admin/experiments/{copy['id']}/uploads").json()
    assert [u["filename"] for u in uploads] == ["with_meta.csv"]
    assert uploads[0]["dataset_meta"] == meta


def test_duplicate_bumps_suffix_on_repeated_copies(client: TestClient):
    created = _create_experiment(client)

    first = client.post(f"/api/admin/experiments/{created['id']}/duplicate").json()
    second = client.post(f"/api/admin/experiments/{created['id']}/duplicate").json()

    assert first["name"] == f"{created['name']} COPY"
    assert second["name"] == f"{created['name']} COPY (2)"


def test_duplicate_preserves_parent_question_links(client: TestClient):
    created = _create_experiment(client)
    _upload_parent_and_children(client, created["id"])

    copy = client.post(f"/api/admin/experiments/{created['id']}/duplicate").json()
    # Parent rows don't count as ratable questions, so only the two children.
    assert copy["question_count"] == 2

    session_payload = _start_session(client, copy["id"], prolific_pid="PID_DUP_PARENT")
    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()
    assert question["question_id"] in {"sub_satisfied", "sub_problem"}
    assert question["parent_question_text"] == PARENT_TEXT


def test_duplicate_finished_experiment_yields_draft(client: TestClient, sync_engine):
    created = _create_experiment(client)
    _mark_experiment_status(sync_engine, created["id"], "FINISHED")

    response = client.post(f"/api/admin/experiments/{created['id']}/duplicate")
    assert response.status_code == 200
    assert response.json()["status"] == "DRAFT"


def test_duplicate_missing_experiment_returns_404(client: TestClient):
    response = client.post("/api/admin/experiments/999999/duplicate")
    assert response.status_code == 404


def _list_entry(client: TestClient, experiment_id: int) -> dict:
    return next(
        item for item in client.get("/api/admin/experiments").json() if item["id"] == experiment_id
    )


@respx.mock
def test_list_experiments_surfaces_pending_action_flag(
    client: TestClient,
    enable_prolific,
):
    """The list endpoint flags experiments with a pending admin action so the
    dashboard can show an attention dot: an unpublished draft round, then
    (after all rounds close with the target unmet) a prompt to launch another
    round. Terminal / actively-collecting states carry no flag."""
    experiment, pilot = _create_prolific_experiment(client)
    _upload_questions(client, experiment["id"])  # 2 questions × target 2 = 4 actions

    # Pilot draft sits UNPUBLISHED → publish it.
    entry = _list_entry(client, experiment["id"])
    assert entry["needs_attention"] is True
    assert "publish" in entry["attention_reason"].lower()

    _mock_publish_study()
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{pilot['id']}/publish")
    _mock_close_study()
    client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{pilot['id']}/close")

    _mock_create_study(study_id="MAIN")
    main_round = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 3},
    ).json()

    _mock_publish_study(study_id="MAIN")
    client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{main_round['id']}/publish"
    )

    # Main round is ACTIVE (still collecting) → nothing to do, no flag.
    entry = _list_entry(client, experiment["id"])
    assert entry["needs_attention"] is False
    assert entry["attention_reason"] is None

    _mock_close_study(study_id="MAIN")
    client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{main_round['id']}/close"
    )

    # All rounds closed, no ratings collected → target unmet → launch another round.
    entry = _list_entry(client, experiment["id"])
    assert entry["needs_attention"] is True
    assert "launch another round" in entry["attention_reason"].lower()

    finish_resp = client.post(f"/api/admin/experiments/{experiment['id']}/finish")
    assert finish_resp.status_code == 200, finish_resp.text

    # Finished experiments are terminal → never flagged.
    entry = _list_entry(client, experiment["id"])
    assert entry["needs_attention"] is False
    assert entry["attention_reason"] is None


# ---------------------------------------------------------------------------
# Question assignment (reservation) behavior
# ---------------------------------------------------------------------------


def _create_experiment_with_target(client: TestClient, target: int) -> dict:
    response = client.post(
        "/api/admin/experiments",
        json={
            "name": _unique_name("experiment"),
            "num_ratings_per_question": target,
        },
    )
    assert response.status_code == 200
    return response.json()


def _get_next_question(client: TestClient, session_payload: dict) -> dict | None:
    response = client.get("/api/raters/next-question", headers=_rater_headers(session_payload))
    assert response.status_code == 200
    return response.json()


def _submit(client: TestClient, session_payload: dict, question: dict) -> None:
    response = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": datetime.now(UTC).isoformat(),
        },
    )
    assert response.status_code == 200, response.text


def test_concurrent_raters_get_distinct_questions_via_reservations(client: TestClient):
    # Target 1 with 2 questions: serving a question reserves its only slot,
    # so a second rater must get the other question even though nothing has
    # been submitted yet. A third rater joins as backfill on the least-covered
    # in-flight question rather than being sent home while ratings are still
    # unsubmitted.
    experiment = _create_experiment_with_target(client, target=1)
    _upload_questions(client, experiment["id"])

    session_a = _start_session(client, experiment["id"], prolific_pid="PID_RES_A")
    question_a = _get_next_question(client, session_a)

    session_b = _start_session(client, experiment["id"], prolific_pid="PID_RES_B")
    question_b = _get_next_question(client, session_b)

    assert question_a is not None and question_b is not None
    assert question_a["id"] != question_b["id"]

    session_c = _start_session(client, experiment["id"], prolific_pid="PID_RES_C")
    question_c = _get_next_question(client, session_c)
    assert question_c is not None
    assert question_c["id"] in {question_a["id"], question_b["id"]}


def _upload_single_question(client: TestClient, experiment_id: int) -> None:
    csv_data = (
        "question_id,question_text,gt_answer,options,question_type\n"
        "only,The only question,Yes,Yes|No,MC"
    )
    response = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("single.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 200


def test_extra_raters_backfill_inflight_question_and_overshoot_is_accepted(client: TestClient):
    # One question, target 1. The first rater reserves the slot; later raters
    # join the same question as backfill (a reserving rater may never submit,
    # and their sessions are already paid for either way).
    experiment = _create_experiment_with_target(client, target=1)
    _upload_single_question(client, experiment["id"])

    sessions = [
        _start_session(client, experiment["id"], prolific_pid=f"PID_BF_{i}") for i in range(3)
    ]
    served = [_get_next_question(client, session) for session in sessions]
    assert all(question is not None for question in served)
    assert len({question["id"] for question in served}) == 1

    # A backfiller submits first: the committed target is met, yet both the
    # original holder's late rating and a fresh in-session rater's extra are
    # still accepted (flagged for truncation in the export).
    _submit(client, sessions[1], served[1])
    _submit(client, sessions[0], served[0])

    session_late = _start_session(client, experiment["id"], prolific_pid="PID_BF_LATE")
    late_question = _get_next_question(client, session_late)
    assert late_question is not None
    _submit(client, session_late, late_question)
    # Only exhaustion ends the session: the late rater has now rated
    # everything, so nothing further is served.
    assert _get_next_question(client, session_late) is None


def test_next_question_reserves_and_is_stable_across_refreshes(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_REFRESH")

    first = _get_next_question(client, session_payload)
    second = _get_next_question(client, session_payload)

    assert first is not None and second is not None
    assert first["id"] == second["id"]


def test_rater_keeps_rating_after_target_until_exhausted(client: TestClient):
    experiment = _create_experiment_with_target(client, target=1)
    _upload_questions(client, experiment["id"])

    session_a = _start_session(client, experiment["id"], prolific_pid="PID_TGT_A")
    for _ in range(2):
        question = _get_next_question(client, session_a)
        assert question is not None
        _submit(client, session_a, question)
    # Rater A has personally rated everything; only then are they done.
    assert _get_next_question(client, session_a) is None

    # Every question is at target, but a rater already in session is paid
    # either way — keep serving them; their extras are flagged in the export
    # and can substitute for quality-filtered ratings.
    session_b = _start_session(client, experiment["id"], prolific_pid="PID_TGT_B")
    for _ in range(2):
        question = _get_next_question(client, session_b)
        assert question is not None
        _submit(client, session_b, question)
    assert _get_next_question(client, session_b) is None


def test_end_session_releases_reserved_slot(client: TestClient):
    experiment = _create_experiment_with_target(client, target=1)
    _upload_questions(client, experiment["id"])

    session_a = _start_session(client, experiment["id"], prolific_pid="PID_REL_A")
    question_a = _get_next_question(client, session_a)
    session_b = _start_session(client, experiment["id"], prolific_pid="PID_REL_B")
    question_b = _get_next_question(client, session_b)
    assert question_a is not None and question_b is not None

    # A walks away without answering; their reserved slot must become
    # servable again immediately.
    end_response = client.post("/api/raters/end-session", headers=_rater_headers(session_a))
    assert end_response.status_code == 200

    session_c = _start_session(client, experiment["id"], prolific_pid="PID_REL_C")
    question_c = _get_next_question(client, session_c)
    assert question_c is not None
    assert question_c["id"] == question_a["id"]

    # The ended rater can't be served (and re-reserve) anything either —
    # mirrors submit_rating's is_active check.
    rejected = client.get("/api/raters/next-question", headers=_rater_headers(session_a))
    assert rejected.status_code == 403


def _start_preview_session(client: TestClient, experiment_id: int, prolific_pid: str) -> dict:
    response = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment_id,
            "PROLIFIC_PID": prolific_pid,
            "STUDY_ID": "preview",
            "SESSION_ID": f"SESSION_{prolific_pid}",
            "preview": "true",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_preview_ratings_do_not_count_toward_target(client: TestClient):
    experiment = _create_experiment_with_target(client, target=1)
    _upload_questions(client, experiment["id"])

    preview_session = _start_preview_session(client, experiment["id"], "PID_PREV_BLOCK")
    for _ in range(2):
        question = _get_next_question(client, preview_session)
        assert question is not None
        _submit(client, preview_session, question)

    # Both questions carry a preview rating, but real work must still be served.
    real_session = _start_session(client, experiment["id"], prolific_pid="PID_PREV_REAL")
    assert _get_next_question(client, real_session) is not None


def test_preview_rater_can_walk_flow_when_target_met(client: TestClient):
    experiment = _create_experiment_with_target(client, target=1)
    _upload_questions(client, experiment["id"])

    real_session = _start_session(client, experiment["id"], prolific_pid="PID_WALK_REAL")
    for _ in range(2):
        question = _get_next_question(client, real_session)
        assert question is not None
        _submit(client, real_session, question)

    # Real raters are done, but a preview session still gets served so the
    # admin can always demo the flow.
    preview_session = _start_preview_session(client, experiment["id"], "PID_WALK_PREV")
    assert _get_next_question(client, preview_session) is not None


def _fetch_question_db_ids(sync_engine, experiment_id: int) -> list[int]:
    with sync_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id FROM questions WHERE experiment_id = :experiment_id ORDER BY id"),
            {"experiment_id": experiment_id},
        ).all()
    return [row[0] for row in rows]


def _seed_rating_from_new_rater(
    sync_engine,
    *,
    experiment_id: int,
    question_db_id: int,
    prolific_id: str,
) -> None:
    with sync_engine.begin() as conn:
        rater_id = conn.execute(
            text(
                """
                INSERT INTO raters (
                    prolific_id, study_id, session_id, experiment_id,
                    session_start, is_active
                ) VALUES (
                    :prolific_id, 'STUDY_SEED', :session_id, :experiment_id,
                    NOW(), true
                ) RETURNING id
                """
            ),
            {
                "prolific_id": prolific_id,
                "session_id": f"SESSION_{prolific_id}",
                "experiment_id": experiment_id,
            },
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO ratings (
                    question_id, rater_id, answer, confidence,
                    time_started, time_submitted
                ) VALUES (:question_id, :rater_id, 'Yes', 3, NOW(), NOW())
                """
            ),
            {"question_id": question_db_id, "rater_id": rater_id},
        )


def test_stats_effective_ratings_cap_overshoot_per_question(client: TestClient, sync_engine):
    # Target 2. q1 overshoots to 3 ratings, q2 has only 1: raw total (4)
    # reads as target met, effective total (min-capped per question) must not.
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    q1_id, q2_id = _fetch_question_db_ids(sync_engine, experiment["id"])

    for index in range(3):
        _seed_rating_from_new_rater(
            sync_engine,
            experiment_id=experiment["id"],
            question_db_id=q1_id,
            prolific_id=f"PID_CAP_Q1_{index}",
        )
    _seed_rating_from_new_rater(
        sync_engine,
        experiment_id=experiment["id"],
        question_db_id=q2_id,
        prolific_id="PID_CAP_Q2_0",
    )

    stats = client.get(f"/api/admin/experiments/{experiment['id']}/stats").json()
    assert stats["total_ratings"] == 4
    assert stats["effective_ratings"] == 3  # min(3, 2) + min(1, 2)
    assert stats["questions_complete"] == 1


def test_export_flags_ratings_beyond_target(client: TestClient, sync_engine):
    # Target 2 with 3 ratings on one question: the first two by submission
    # order count toward the target, the third is flagged for truncation.
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    q1_id, _q2_id = _fetch_question_db_ids(sync_engine, experiment["id"])

    for index in range(3):
        _seed_rating_from_new_rater(
            sync_engine,
            experiment_id=experiment["id"],
            question_db_id=q1_id,
            prolific_id=f"PID_TRUNC_{index}",
        )

    with client.stream("GET", f"/api/admin/experiments/{experiment['id']}/export") as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    rows = list(csv.DictReader(io.StringIO(body)))
    assert len(rows) == 3
    rows.sort(key=lambda row: int(row["rating_id"]))
    assert [row["counts_toward_target"] for row in rows] == ["True", "True", "False"]


@respx.mock
def test_submit_auto_stops_active_round_when_target_met(client: TestClient, enable_prolific):
    experiment, pilot = _create_prolific_experiment(client)
    # Upload before publishing: the first publish locks experiment config.
    _upload_questions(client, experiment["id"])
    _mock_publish_study()
    publish_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/{pilot['id']}/publish"
    )
    assert publish_resp.status_code == 200

    stop_route = _mock_close_study()

    # Target 2 × 2 questions: two raters each rating both questions meet the
    # target on the final submit, which must stop the still-ACTIVE study.
    for prolific_pid in ("PID_STOP_A", "PID_STOP_B"):
        session_payload = _start_session(client, experiment["id"], prolific_pid=prolific_pid)
        for _ in range(2):
            question = _get_next_question(client, session_payload)
            assert question is not None
            _submit(client, session_payload, question)

    assert stop_route.called

    rounds = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/rounds").json()
    assert rounds[0]["prolific_study_status"] == "AWAITING_REVIEW"
