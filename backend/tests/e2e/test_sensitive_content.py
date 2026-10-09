"""Sensitive-content studies and pinned terms.

An experiment picks a terms bundle and may declare a Prolific content
warning: the warning goes to Prolific (fields, prescreener, description), the
bundle's consent carries the details and a debrief closes the session, and
publishing pins the statement versions so later edits at the source never
reach a running study.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from config import get_settings

from test_consent import _consent, _create_experiment, _start, _stored_consent, _upload


PROLIFIC_BASE = "https://api.prolific.com/api/v1"
STUDY_ID = "study-sensitive-1"


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


# ── choosing a bundle and a warning ───────────────────────────────────────


def test_experiment_defaults_to_standard_terms(client: TestClient):
    experiment_id = _create_experiment(client)
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["content_warning"] == "none"
    assert detail["content_warning_details"] is None
    assert detail["terms_bundle"] == "standard"
    assert detail["consent_statement_ref"] is None
    assert detail["debrief_statement_ref"] is None


def test_content_warning_requires_details_and_a_bundle_that_serves_it(
    client: TestClient, terms_source
):
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


def test_sensitive_study_serves_warning_consent_and_debrief(
    client: TestClient, sync_engine, terms_source
):
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
    assert (stored["bundle"], stored["version"]) == ("sensitive", 1)
    assert "Some passages describe violence." in stored["rendered_text"]


def test_ordinary_study_has_no_debrief(client: TestClient, terms_source):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    payload = _start(client, experiment_id, "PID_PLAIN")
    assert payload["content_warning"] == "none"
    assert payload["debrief_html"] is None


def test_explicit_uses_its_own_bundle(client: TestClient, terms_source):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _make_sensitive(client, experiment_id, level="explicit")
    payload = _start(client, experiment_id, "PID_EXPLICIT")
    assert payload["content_warning"] == "explicit"
    assert "contains explicit content" in payload["consent_statement_html"]


def test_terms_status_and_preview_endpoints(client: TestClient, terms_source):
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


def test_unset_source_reports_not_ok_and_previews_the_placeholder(client: TestClient):
    assert get_settings().terms.source_url == ""
    status = client.get("/api/admin/terms").json()
    assert status["ok"] is False
    assert "TERMS__SOURCE_URL is not set" in status["error"]
    assert status["bundles"] == []

    experiment_id = _create_experiment(client)
    preview = client.get(f"/api/admin/experiments/{experiment_id}/terms/preview").json()
    assert preview["pinned"] is False
    assert preview["consent_ref"] == "placeholder v0"
    assert "Consent statement not configured" in preview["consent_html"]


def test_unreadable_source_reports_but_lets_admins_save(client: TestClient, terms_source: Path):
    experiment_id = _create_experiment(client)
    get_settings().terms.source_url = f"file://{terms_source}/does-not-exist"

    status = client.get("/api/admin/terms").json()
    assert status["ok"] is False
    assert "manifest.json" in status["error"]

    # Saving is best effort: the bundle check defers to publish.
    assert _patch(client, experiment_id, terms_bundle="standard").status_code == 200


# ── Prolific ──────────────────────────────────────────────────────────────


@respx.mock
def test_sensitive_study_sends_warning_prescreener_and_description(
    client: TestClient, enable_prolific, terms_source
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
def test_ordinary_study_payload_carries_no_warning_fields(
    client: TestClient, enable_prolific, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    create_route = _mock_prolific_study()

    _run_pilot(client, experiment_id)

    sent = json.loads(create_route.calls.last.request.content)
    assert "content_warnings" not in sent
    assert "content_warning_details" not in sent
    assert not sent["description"].startswith("<p><b>Content warning")
    assert all(f["filter_id"] != "harmful-content" for f in sent.get("filters", []))


# ── pinning at first publish ──────────────────────────────────────────────


@respx.mock
def test_publish_pins_versions_and_running_studies_keep_them(
    client: TestClient, enable_prolific, terms_source: Path, sync_engine
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

    # The consent record names the pinned version and the archived file.
    _consent(client, rater)
    stored = _stored_consent(sync_engine, rater["rater_id"])
    assert (stored["bundle"], stored["version"]) == ("standard", 1)
    assert stored["source_url"].endswith("consent/standard/v1.md")


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


@respx.mock
def test_publish_without_a_source_pins_nothing_and_keeps_the_placeholder(
    client: TestClient, enable_prolific
):
    assert get_settings().terms.source_url == ""
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _mock_prolific_study()
    pilot = _run_pilot(client, experiment_id)

    published = _publish(client, experiment_id, pilot["id"])
    assert published.status_code == 200, published.text
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["status"] == "LAUNCH"
    assert detail["consent_statement_ref"] is None

    rater = _start(client, experiment_id, "PID_NOSOURCE")
    assert "Consent statement not configured" in rater["consent_statement_html"]


@respx.mock
def test_failed_publish_pins_nothing_and_leaves_terms_editable(
    client: TestClient, enable_prolific, terms_source: Path
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _make_sensitive(client, experiment_id)
    _mock_prolific_study()
    pilot = _run_pilot(client, experiment_id)
    respx.post(f"{PROLIFIC_BASE}/studies/{STUDY_ID}/transition/").mock(
        return_value=Response(502, json={"error": "upstream"})
    )

    refused = _publish(client, experiment_id, pilot["id"])
    assert refused.status_code == 502
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["status"] == "DRAFT"
    assert detail["consent_statement_ref"] is None
    assert detail["debrief_statement_ref"] is None

    # Still DRAFT, so the admin can change the bundle, and the retry pins
    # what the experiment says now, not what it said when the first attempt
    # failed.
    _make_sensitive(client, experiment_id, level="explicit")
    respx.post(f"{PROLIFIC_BASE}/studies/{STUDY_ID}/transition/").mock(
        return_value=Response(200, json={"id": STUDY_ID, "status": "ACTIVE"})
    )
    assert _publish(client, experiment_id, pilot["id"]).status_code == 200
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["consent_statement_ref"] == "explicit v1"
    assert detail["debrief_statement_ref"] == "explicit v1"
    rater = _start(client, experiment_id, "PID_RETRY")
    assert "contains explicit content" in rater["consent_statement_html"]


@respx.mock
def test_downgrading_the_warning_before_publish_clears_it_on_prolific(
    client: TestClient, enable_prolific, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _make_sensitive(client, experiment_id)
    _mock_prolific_study()
    pilot = _run_pilot(client, experiment_id)
    update_route = respx.patch(f"{PROLIFIC_BASE}/studies/{STUDY_ID}/").mock(
        return_value=Response(200, json={"id": STUDY_ID, "status": "UNPUBLISHED"})
    )

    cleared = _patch(client, experiment_id, content_warning="none", terms_bundle="standard")
    assert cleared.status_code == 200, cleared.text
    assert _publish(client, experiment_id, pilot["id"]).status_code == 200

    sent = json.loads(update_route.calls.last.request.content)
    assert sent["content_warnings"] == []
    assert sent["content_warning_details"] is None
    assert not sent["description"].startswith("<p><b>Content warning")
    assert all(f["filter_id"] != "harmful-content" for f in sent["filters"])
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["consent_statement_ref"] == "standard v1"
    assert detail["debrief_statement_ref"] is None
    rater = _start(client, experiment_id, "PID_DOWNGRADED")
    assert rater["debrief_html"] is None


def test_preview_renders_an_unsaved_selection_until_pinned(client: TestClient, terms_source):
    experiment_id = _create_experiment(client)
    # Saved as standard; the admin is trying explicit in the form.
    preview = client.get(
        f"/api/admin/experiments/{experiment_id}/terms/preview",
        params={
            "terms_bundle": "explicit",
            "content_warning": "explicit",
            "content_warning_details": "Graphic descriptions.",
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["consent_ref"] == "explicit v1"
    assert "contains explicit content" in body["consent_html"]
    assert "Graphic descriptions." in body["consent_html"]
    assert body["debrief_ref"] == "explicit v1"
    # Nothing was saved.
    assert (
        client.get(f"/api/admin/experiments/{experiment_id}").json()["terms_bundle"] == "standard"
    )

    # A selection the manifest cannot serve is refused, not silently swapped.
    mismatch = client.get(
        f"/api/admin/experiments/{experiment_id}/terms/preview",
        params={"terms_bundle": "standard", "content_warning": "sensitive"},
    )
    assert mismatch.status_code == 400
    assert "does not serve" in mismatch.json()["detail"]


@respx.mock
def test_preview_ignores_the_selection_once_pinned(
    client: TestClient, enable_prolific, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _mock_prolific_study()
    pilot = _run_pilot(client, experiment_id)
    assert _publish(client, experiment_id, pilot["id"]).status_code == 200

    preview = client.get(
        f"/api/admin/experiments/{experiment_id}/terms/preview",
        params={"terms_bundle": "explicit", "content_warning": "explicit"},
    ).json()
    assert preview["pinned"] is True
    assert preview["consent_ref"] == "standard v1"
    assert preview["debrief_ref"] is None
