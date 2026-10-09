"""Rater terms and consent.

Statements come from the terms source (dummy fixtures under tests/fixtures), consent
is recorded per rater against the exact text shown, study content is refused
until then, publishing pins the statement versions, and a study with a
content warning gets the Prolific treatment.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
TERMS_DIR = BACKEND_DIR / "tests" / "fixtures" / "rater_terms"
PROLIFIC_BASE = "https://api.prolific.com/api/v1"
STUDY_ID = "study-consent-1"

_CSV_ROWS = (
    "question_id,question_text,gt_answer,options,question_type\nq1,Is this useful?,Yes,Yes|No,MC\n"
)


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def terms_source(tmp_path: Path):
    """A private copy of the shipped terms so a test can edit files and the
    manifest without touching the checked-in example."""
    source = tmp_path / "terms"
    shutil.copytree(TERMS_DIR, source)
    settings = get_settings()
    original = settings.terms.source_url
    settings.terms.source_url = f"file://{source}"
    yield source
    settings.terms.source_url = original


@pytest.fixture
def enable_prolific():
    settings = get_settings()
    original_token = settings.prolific.api_token
    original_project = settings.prolific.project_id
    settings.prolific.api_token = "test-token"
    settings.prolific.project_id = "test-project"
    yield settings
    settings.prolific.api_token = original_token
    settings.prolific.project_id = original_project


# ── helpers ───────────────────────────────────────────────────────────────


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


def _patch(client: TestClient, experiment_id: int, **fields) -> Response:
    return client.patch(
        f"/api/admin/experiments/{experiment_id}",
        json={"assistance_method": "none", **fields},
    )


def _make_sensitive(client: TestClient, experiment_id: int, level: str = "sensitive") -> dict:
    response = _patch(
        client,
        experiment_id,
        content_warning=level,
        content_warning_details="Some passages describe violence.",
        terms_bundle=level,
    )
    assert response.status_code == 200, response.text
    return response.json()


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
                """
                SELECT c.accepted_at, c.rendered_text, c.is_preview, s.bundle, s.kind, s.version
                FROM consent_records c JOIN terms_statements s ON s.id = c.statement_id
                WHERE c.rater_id = :id
                """
            ),
            {"id": rater_id},
        ).one_or_none()
    if row is None:
        return None
    return {
        "accepted_at": row[0],
        "rendered_text": row[1],
        "is_preview": row[2],
        "bundle": row[3],
        "kind": row[4],
        "version": row[5],
    }


def _mock_prolific_study(study_id: str = STUDY_ID) -> respx.Route:
    respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(
        return_value=Response(200, json={"id": f"group-{uuid4().hex[:6]}"})
    )
    respx.get(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=Response(200, json={"id": study_id, "status": "UNPUBLISHED"})
    )
    respx.patch(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=Response(200, json={"id": study_id, "status": "UNPUBLISHED"})
    )
    respx.post(f"{PROLIFIC_BASE}/studies/{study_id}/transition/").mock(
        return_value=Response(200, json={"id": study_id, "status": "ACTIVE"})
    )
    return respx.post(f"{PROLIFIC_BASE}/studies/").mock(
        return_value=Response(200, json={"id": study_id, "status": "UNPUBLISHED"})
    )


def _run_pilot(client: TestClient, experiment_id: int) -> dict:
    response = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/pilot",
        json={
            "description": "Test study",
            "estimated_completion_time": 10,
            "reward": 500,
            "pilot_places": 5,
            "device_compatibility": ["desktop"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client: TestClient, experiment_id: int, round_id: int) -> Response:
    return client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/{round_id}/publish")


# ── the consent screen and gate ───────────────────────────────────────────


def test_start_serves_bundle_statement_with_placeholders_filled(client: TestClient):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)

    payload = _start(client, experiment_id, "PID_DEFAULT")

    assert payload["consented_at"] is None
    assert payload["content_warning"] == "none"
    assert payload["debrief_html"] is None
    html = payload["consent_statement_html"]
    assert "<h2>Purpose of the study</h2>" in html
    assert "<b>I agree</b>" in html
    # {{session_length}} rendered from the experiment's 60-minute default.
    assert "You have 1 hour." in html
    assert "{{" not in html


def test_study_content_is_refused_until_consent(client: TestClient):
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


def test_consent_records_version_and_rendered_text_and_is_idempotent(
    client: TestClient, sync_engine
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    payload = _start(client, experiment_id, "PID_TEXT")

    first = _consent(client, payload)
    second = _consent(client, payload)
    assert first["consented_at"] == second["consented_at"]

    stored = _stored_consent(sync_engine, payload["rater_id"])
    assert stored is not None
    assert (stored["bundle"], stored["kind"], stored["version"]) == ("standard", "consent", 1)
    assert stored["is_preview"] is False
    # The text as shown, placeholders filled, not the raw file.
    assert "You have 1 hour." in stored["rendered_text"]
    assert "{{" not in stored["rendered_text"]

    # Coming back through Prolific's link resumes the session already consented.
    resumed = _start(client, experiment_id, "PID_TEXT")
    assert resumed["consented_at"] is not None


def test_preview_reset_asks_for_consent_again(client: TestClient, sync_engine):
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


# ── configuring an experiment's terms ─────────────────────────────────────


def test_experiment_defaults_to_standard_terms(client: TestClient):
    experiment_id = _create_experiment(client)
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["content_warning"] == "none"
    assert detail["content_warning_details"] is None
    assert detail["terms_bundle"] == "standard"
    assert detail["consent_statement_ref"] is None
    assert detail["debrief_statement_ref"] is None


def test_content_warning_requires_details_and_a_bundle_that_serves_it(client: TestClient):
    experiment_id = _create_experiment(client)

    missing_details = _patch(client, experiment_id, content_warning="sensitive")
    assert missing_details.status_code == 400
    assert "details are required" in missing_details.json()["detail"]

    wrong_bundle = _patch(
        client, experiment_id, content_warning="sensitive", content_warning_details="Violence."
    )
    assert wrong_bundle.status_code == 400
    assert "does not serve" in wrong_bundle.json()["detail"]

    unknown_bundle = _patch(client, experiment_id, terms_bundle="nope")
    assert unknown_bundle.status_code == 400
    assert "not in the manifest" in unknown_bundle.json()["detail"]

    body = _make_sensitive(client, experiment_id)
    assert body["content_warning"] == "sensitive"
    assert body["terms_bundle"] == "sensitive"
    assert body["content_warning_details"] == "Some passages describe violence."


def test_sensitive_study_serves_warning_consent_and_debrief(client: TestClient, sync_engine):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _make_sensitive(client, experiment_id)

    payload = _start(client, experiment_id, "PID_SENSITIVE")
    assert payload["content_warning"] == "sensitive"
    consent_html = payload["consent_statement_html"]
    assert "<h2>Content warning</h2>" in consent_html
    assert "Some passages describe violence." in consent_html
    assert payload["debrief_html"] is not None
    assert "Continue to Prolific" in payload["debrief_html"]
    # {{study_name}} in the debrief.
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["name"] in payload["debrief_html"]

    _consent(client, payload)
    stored = _stored_consent(sync_engine, payload["rater_id"])
    assert stored["bundle"] == "sensitive"
    assert "Some passages describe violence." in stored["rendered_text"]


def test_explicit_uses_its_own_bundle(client: TestClient):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _make_sensitive(client, experiment_id, level="explicit")
    payload = _start(client, experiment_id, "PID_EXPLICIT")
    assert payload["content_warning"] == "explicit"
    assert "contains explicit content" in payload["consent_statement_html"]


def test_terms_status_and_preview_endpoints(client: TestClient):
    status = client.get("/api/admin/terms")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["ok"] is True
    assert {b["key"] for b in body["bundles"]} == {"standard", "sensitive", "explicit"}
    sensitive = next(b for b in body["bundles"] if b["key"] == "sensitive")
    assert sensitive["content_warnings"] == ["sensitive"]
    assert sensitive["debrief_version"] == 1

    experiment_id = _create_experiment(client)
    _make_sensitive(client, experiment_id)
    preview = client.get(f"/api/admin/experiments/{experiment_id}/terms/preview")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["pinned"] is False
    assert body["consent_ref"] == "sensitive v1"
    assert body["debrief_ref"] == "sensitive v1"
    assert "Some passages describe violence." in body["consent_html"]
    assert "Continue to Prolific" in body["debrief_html"]


def test_unreadable_source_blocks_raters_and_reports_but_lets_admins_save(
    client: TestClient, terms_source: Path
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    get_settings().terms.source_url = f"file://{terms_source}/does-not-exist"

    status = client.get("/api/admin/terms").json()
    assert status["ok"] is False
    assert "manifest.json" in status["error"]

    # Saving is best effort: the bundle check defers to publish.
    assert _patch(client, experiment_id, terms_bundle="standard").status_code == 200

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


# ── Prolific and pinning ──────────────────────────────────────────────────


@respx.mock
def test_sensitive_study_sends_warning_prescreener_and_description(
    client: TestClient, enable_prolific
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _make_sensitive(client, experiment_id)
    create_route = _mock_prolific_study()

    _run_pilot(client, experiment_id)

    sent = json.loads(create_route.calls.last.request.content)
    assert sent["content_warnings"] == ["sensitive"]
    assert sent["content_warning_details"] == "Some passages describe violence."
    assert sent["description"].startswith("<p><b>Content warning:</b> Some passages describe")
    assert {"filter_id": "harmful-content", "selected_values": ["0"]} in sent["filters"]


@respx.mock
def test_ordinary_study_payload_carries_no_warning_fields(client: TestClient, enable_prolific):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    create_route = _mock_prolific_study()

    _run_pilot(client, experiment_id)

    sent = json.loads(create_route.calls.last.request.content)
    assert "content_warnings" not in sent
    assert "content_warning_details" not in sent
    assert not sent["description"].startswith("<p><b>Content warning")
    assert all(f["filter_id"] != "harmful-content" for f in sent.get("filters", []))


@respx.mock
def test_publish_pins_versions_and_running_studies_keep_them(
    client: TestClient, enable_prolific, terms_source: Path
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _mock_prolific_study()
    pilot = _run_pilot(client, experiment_id)
    assert (
        client.get(f"/api/admin/experiments/{experiment_id}").json()["consent_statement_ref"]
        is None
    )

    published = _publish(client, experiment_id, pilot["id"])
    assert published.status_code == 200, published.text
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["status"] == "LAUNCH"
    assert detail["consent_statement_ref"] == "standard v1"
    assert detail["debrief_statement_ref"] is None

    # Terms are locked with the rest of the config now.
    locked = _patch(client, experiment_id, terms_bundle="sensitive")
    assert locked.status_code == 400
    assert "terms_bundle" in locked.json()["detail"]

    # A new version at the source: new experiments pick it up on their next
    # read, the published one keeps serving the pinned copy.
    (terms_source / "consent" / "standard" / "v2.md").write_text(
        "## Version two\n\nYou have {{session_length}}.\n", encoding="utf-8"
    )
    manifest = json.loads((terms_source / "manifest.json").read_text(encoding="utf-8"))
    manifest["bundles"]["standard"]["consent"] = 2
    (terms_source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    fresh_id = _create_experiment(client)
    fresh_preview = client.get(f"/api/admin/experiments/{fresh_id}/terms/preview").json()
    assert fresh_preview["consent_ref"] == "standard v2"
    assert "<h2>Version two</h2>" in fresh_preview["consent_html"]

    pinned_preview = client.get(f"/api/admin/experiments/{experiment_id}/terms/preview").json()
    assert pinned_preview["pinned"] is True
    assert pinned_preview["consent_ref"] == "standard v1"
    rater = _start(client, experiment_id, "PID_PINNED")
    assert "<h2>Purpose of the study</h2>" in rater["consent_statement_html"]
    assert "Version two" not in rater["consent_statement_html"]


@respx.mock
def test_publish_refuses_a_version_that_changed_at_the_source(
    client: TestClient, enable_prolific, terms_source: Path
):
    # First experiment archives standard v1 as it is.
    first_id = _create_experiment(client)
    _upload(client, first_id)
    _mock_prolific_study(study_id="study-first")
    _run_pilot(client, first_id)
    assert (
        _publish(
            client,
            first_id,
            client.get(f"/api/admin/experiments/{first_id}/prolific/rounds").json()[0]["id"],
        ).status_code
        == 200
    )

    # Someone edits v1 in place instead of publishing v2.
    path = terms_source / "consent" / "standard" / "v1.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nEdited in place.\n", encoding="utf-8")

    second_id = _create_experiment(client)
    _upload(client, second_id)
    _mock_prolific_study(study_id="study-second")
    _run_pilot(client, second_id)
    round_id = client.get(f"/api/admin/experiments/{second_id}/prolific/rounds").json()[0]["id"]
    refused = _publish(client, second_id, round_id)
    assert refused.status_code == 400
    assert "changed at the source" in refused.json()["detail"]
    assert "publish v2 instead" in refused.json()["detail"]
    assert client.get(f"/api/admin/experiments/{second_id}").json()["status"] == "DRAFT"


@respx.mock
def test_publish_refuses_when_source_is_unreadable(
    client: TestClient, enable_prolific, terms_source: Path
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _mock_prolific_study()
    pilot = _run_pilot(client, experiment_id)
    get_settings().terms.source_url = f"file://{terms_source}/gone"

    refused = _publish(client, experiment_id, pilot["id"])
    assert refused.status_code == 400
    assert "Rater terms" in refused.json()["detail"]
    assert client.get(f"/api/admin/experiments/{experiment_id}").json()["status"] == "DRAFT"


# ── reporting ─────────────────────────────────────────────────────────────


def test_export_and_analytics_carry_the_consent_version(client: TestClient):
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
