"""Rater consent.

The statement comes from the terms source (generated here at runtime; the
repository ships none), every rater agrees to it before anything else, the
agreement is recorded against the exact text shown, and study content is
refused until then.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from config import get_settings

from terms_helpers import write_terms_source

_CSV_ROWS = (
    "question_id,question_text,gt_answer,options,question_type\nq1,Is this useful?,Yes,Yes|No,MC\n"
)


def _create_experiment(client: TestClient) -> int:
    response = client.post(
        "/api/admin/experiments",
        json={"name": f"consent-{uuid4().hex[:8]}", "num_ratings_per_question": 1},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _upload(client: TestClient, experiment_id: int) -> None:
    response = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("questions.csv", _CSV_ROWS, "text/csv")},
    )
    assert response.status_code == 200, response.text


def _start(client: TestClient, experiment_id: int, pid: str, preview: bool = False) -> dict:
    response = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment_id,
            "PROLIFIC_PID": pid,
            "STUDY_ID": "STUDY_1",
            "SESSION_ID": f"SESSION_{pid}",
            "preview": preview,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _headers(payload: dict) -> dict[str, str]:
    return {"X-Rater-Session": payload["rater_session_token"]}


def _consent(client: TestClient, payload: dict) -> dict:
    response = client.post("/api/raters/consent", headers=_headers(payload))
    assert response.status_code == 200, response.text
    return response.json()


def _stored_consent(sync_engine, rater_id: int) -> dict | None:
    with sync_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT accepted_at, rendered_text, is_preview, bundle, version, sha256, "
                "source_url FROM consent_records WHERE rater_id = :id"
            ),
            {"id": rater_id},
        ).one_or_none()
    if row is None:
        return None
    keys = (
        "accepted_at",
        "rendered_text",
        "is_preview",
        "bundle",
        "version",
        "sha256",
        "source_url",
    )
    return dict(zip(keys, row, strict=True))


def test_start_serves_the_statement_with_placeholders_filled(client: TestClient, terms_source):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)

    payload = _start(client, experiment_id, "PID_DEFAULT")

    assert payload["consented_at"] is None
    html = payload["consent_statement_html"]
    assert "<h2>Purpose of the study</h2>" in html
    assert "<b>I agree</b>" in html
    # {{session_length}} rendered from the experiment's 60-minute default.
    assert "You have 1 hour." in html
    assert "{{" not in html


def test_unset_source_serves_a_placeholder_and_records_it_as_such(client: TestClient, sync_engine):
    # No terms_source fixture: TERMS__SOURCE_URL is unset in the test stack.
    assert get_settings().terms.source_url == ""
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)

    payload = _start(client, experiment_id, "PID_PLACEHOLDER")
    html = payload["consent_statement_html"]
    assert "<h2>Consent statement not configured</h2>" in html
    assert "TERMS__SOURCE_URL" in html
    assert "{{" not in html

    _consent(client, payload)
    stored = _stored_consent(sync_engine, payload["rater_id"])
    assert (stored["bundle"], stored["version"], stored["source_url"]) == ("placeholder", 0, "")
    assert "Consent statement not configured" in stored["rendered_text"]


def test_study_content_is_refused_until_consent(client: TestClient, terms_source):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    payload = _start(client, experiment_id, "PID_GATE")
    headers = _headers(payload)

    for attempt in (
        lambda: client.get("/api/raters/next-question", headers=headers),
        lambda: client.get("/api/raters/questions/1", headers=headers),
        lambda: client.post(
            "/api/raters/submit",
            headers=headers,
            json={
                "question_id": 1,
                "answer": "Yes",
                "confidence": 3,
                "time_started": "2026-09-30T00:00:00Z",
            },
        ),
        lambda: client.post(
            "/api/raters/assistance/start", headers=headers, json={"question_id": 1}
        ),
    ):
        response = attempt()
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Consent required"

    # Session bookkeeping still works without consent, so a rater who closes
    # the tab on the consent screen can be cleaned up.
    assert client.get("/api/raters/session-status", headers=headers).status_code == 200

    _consent(client, payload)
    served = client.get("/api/raters/next-question", headers=headers)
    assert served.status_code == 200
    assert served.json()["question_text"] == "Is this useful?"


def test_consent_records_the_text_shown_and_its_source_and_is_idempotent(
    client: TestClient, sync_engine, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    payload = _start(client, experiment_id, "PID_TEXT")

    first = _consent(client, payload)
    second = _consent(client, payload)
    assert first["consented_at"] == second["consented_at"]

    stored = _stored_consent(sync_engine, payload["rater_id"])
    assert stored is not None
    assert (stored["bundle"], stored["version"]) == ("standard", 1)
    assert stored["source_url"].endswith("consent/standard/v1.md")
    assert len(stored["sha256"]) == 64
    assert stored["is_preview"] is False
    # The text as shown, placeholders filled, not the raw file.
    assert "You have 1 hour." in stored["rendered_text"]
    assert "{{" not in stored["rendered_text"]

    # Coming back through Prolific's link resumes the session already consented.
    resumed = _start(client, experiment_id, "PID_TEXT")
    assert resumed["consented_at"] is not None


def test_preview_reset_asks_for_consent_again(client: TestClient, sync_engine, terms_source):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)

    first = _start(client, experiment_id, "PID_PREVIEW", preview=True)
    _consent(client, first)
    assert _stored_consent(sync_engine, first["rater_id"])["is_preview"] is True
    assert client.get("/api/raters/next-question", headers=_headers(first)).status_code == 200

    # Every restart of a preview rater starts the flow over, consent screen
    # included, so the admin sees exactly what a new rater sees.
    again = _start(client, experiment_id, "PID_PREVIEW", preview=True)
    assert again["consented_at"] is None
    assert _stored_consent(sync_engine, again["rater_id"]) is None
    assert client.get("/api/raters/next-question", headers=_headers(again)).status_code == 403


def test_a_new_statement_version_is_served_on_the_next_read(
    client: TestClient, terms_source: Path, sync_engine
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    write_terms_source(
        terms_source,
        consent_version=2,
        body="## Version two\n\nYou have {{session_length}}. Click **I agree**.\n",
    )

    payload = _start(client, experiment_id, "PID_V2")
    assert "<h2>Version two</h2>" in payload["consent_statement_html"]
    _consent(client, payload)
    stored = _stored_consent(sync_engine, payload["rater_id"])
    assert stored["version"] == 2
    assert "Version two" in stored["rendered_text"]


def test_unreadable_source_refuses_rater_sessions(client: TestClient, terms_source: Path):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    get_settings().terms.source_url = f"file://{terms_source}/does-not-exist"

    start = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment_id,
            "PROLIFIC_PID": "PID_DOWN",
            "STUDY_ID": "S",
            "SESSION_ID": "S1",
        },
    )
    assert start.status_code == 503
    assert "unavailable" in start.json()["detail"]


def test_export_and_analytics_carry_the_consent_version(client: TestClient, terms_source):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    payload = _start(client, experiment_id, "PID_EXPORT")
    _consent(client, payload)
    question = client.get("/api/raters/next-question", headers=_headers(payload)).json()
    submitted = client.post(
        "/api/raters/submit",
        headers=_headers(payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": (datetime.now(UTC) - timedelta(seconds=20)).isoformat(),
        },
    )
    assert submitted.status_code == 200, submitted.text

    export = client.get(f"/api/admin/experiments/{experiment_id}/export")
    assert export.status_code == 200, export.text
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == 1
    assert rows[0]["rater_consent_version"] == "standard v1"
    assert rows[0]["rater_consented_at"]

    analytics = client.get(f"/api/admin/experiments/{experiment_id}/analytics").json()
    rater = analytics["raters"][0]
    assert rater["consent_version"] == "standard v1"
    assert rater["consented_at"]
