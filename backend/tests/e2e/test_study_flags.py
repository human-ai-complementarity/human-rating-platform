"""Study flags: sensitive, explicit and deception, and what follows from them.

A flag set drives what goes to Prolific (warning fields, description prefix,
forced prescreeners), which consent statement raters see, whether the study
ends on a debrief, and what the first publish pins.
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
STUDY_ID = "study-flags-1"
DETAILS = "Some passages describe violence."


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


def _flag(
    client: TestClient, experiment_id: int, *flags: str, details: str | None = DETAILS
) -> dict:
    body = {"study_flags": list(flags)}
    if details is not None:
        body["content_warning_details"] = details
    response = _patch(client, experiment_id, **body)
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
    # The launch gate wants rater instructions before a pilot can run.
    instructions = _patch(client, experiment_id, description="Answer each question.")
    assert instructions.status_code == 200, instructions.text
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


def _filters(sent: dict) -> set[str]:
    return {f["filter_id"] for f in sent.get("filters", [])}


# ── setting flags ─────────────────────────────────────────────────────────


def test_experiment_defaults_to_no_flags(client: TestClient):
    experiment_id = _create_experiment(client)
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["study_flags"] == []
    assert detail["content_warning_details"] is None
    assert detail["consent_statement_ref"] is None
    assert detail["debrief_statement_ref"] is None


def test_flags_can_be_set_at_create(client: TestClient):
    response = client.post(
        "/api/admin/experiments",
        json={
            "name": f"flags-{uuid4().hex[:8]}",
            "internal_name": "flags internal",
            "num_ratings_per_question": 1,
            "study_flags": ["deception", "sensitive"],
            "content_warning_details": DETAILS,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["study_flags"] == ["sensitive", "deception"]
    assert response.json()["content_warning_details"] == DETAILS

    missing = client.post(
        "/api/admin/experiments",
        json={"name": "no details", "num_ratings_per_question": 1, "study_flags": ["explicit"]},
    )
    assert missing.status_code == 400
    assert "details are required" in missing.json()["detail"]


def test_content_flags_require_details_and_unknown_flags_are_refused(client: TestClient):
    experiment_id = _create_experiment(client)

    missing_details = _patch(client, experiment_id, study_flags=["sensitive"])
    assert missing_details.status_code == 400
    assert "details are required" in missing_details.json()["detail"]

    unknown = _patch(client, experiment_id, study_flags=["scary"])
    assert unknown.status_code == 422

    # Deception alone needs no details.
    body = _flag(client, experiment_id, "deception", details=None)
    assert body["study_flags"] == ["deception"]

    body = _flag(client, experiment_id, "explicit", "deception")
    assert body["study_flags"] == ["explicit", "deception"]
    assert body["content_warning_details"] == DETAILS


def test_duplicate_copies_the_flags(client: TestClient):
    experiment_id = _create_experiment(client)
    _flag(client, experiment_id, "sensitive")
    copy = client.post(f"/api/admin/experiments/{experiment_id}/duplicate")
    assert copy.status_code == 200, copy.text
    assert copy.json()["study_flags"] == ["sensitive"]
    assert copy.json()["content_warning_details"] == DETAILS


# ── what raters see ───────────────────────────────────────────────────────


def test_sensitive_study_serves_the_warned_consent_and_no_debrief(
    client: TestClient, sync_engine, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _flag(client, experiment_id, "sensitive")

    payload = _start(client, experiment_id, "PID_SENSITIVE")
    assert payload["study_flags"] == ["sensitive"]
    consent_html = payload["consent_statement_html"]
    assert "<h2>Content warning</h2>" in consent_html
    assert DETAILS in consent_html
    assert payload["debrief_html"] is None

    _consent(client, payload)
    stored = _stored_consent(sync_engine, payload["rater_id"])
    assert (stored["statement"], stored["version"]) == ("warned", 1)
    assert stored["source_url"].endswith("consent/warned/v1.md")
    assert DETAILS in stored["rendered_text"]


def test_deception_study_serves_the_standard_consent_and_a_debrief(
    client: TestClient, sync_engine, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _flag(client, experiment_id, "deception", details=None)

    payload = _start(client, experiment_id, "PID_DECEPTION")
    assert payload["study_flags"] == ["deception"]
    assert "<h2>Content warning</h2>" not in payload["consent_statement_html"]
    assert payload["debrief_html"] is not None
    assert "Continue to Prolific" in payload["debrief_html"]
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["name"] in payload["debrief_html"]

    _consent(client, payload)
    assert _stored_consent(sync_engine, payload["rater_id"])["statement"] == "standard"


def test_ordinary_study_has_no_debrief(client: TestClient, terms_source):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    payload = _start(client, experiment_id, "PID_PLAIN")
    assert payload["study_flags"] == []
    assert payload["debrief_html"] is None


# ── admin endpoints ───────────────────────────────────────────────────────


def test_terms_status_and_preview_endpoints(client: TestClient, terms_source):
    status = client.get("/api/admin/terms")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["ok"] is True
    assert body["documents"] == {"consent/standard": 1, "consent/warned": 1, "debrief": 1}

    experiment_id = _create_experiment(client)
    _flag(client, experiment_id, "explicit", "deception")
    preview = client.get(f"/api/admin/experiments/{experiment_id}/terms/preview")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["pinned"] is False
    assert body["consent_ref"] == "warned v1"
    assert body["debrief_ref"] == "debrief v1"
    assert DETAILS in body["consent_html"]
    assert "Continue to Prolific" in body["debrief_html"]


def test_preview_renders_an_unsaved_selection_until_pinned(client: TestClient, terms_source):
    experiment_id = _create_experiment(client)
    # Saved with no flags; the admin is trying deception plus explicit in the form.
    preview = client.get(
        f"/api/admin/experiments/{experiment_id}/terms/preview",
        params=[
            ("study_flags", "explicit"),
            ("study_flags", "deception"),
            ("content_warning_details", "Graphic descriptions."),
        ],
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["consent_ref"] == "warned v1"
    assert "Graphic descriptions." in body["consent_html"]
    assert body["debrief_ref"] == "debrief v1"
    # Nothing was saved.
    assert client.get(f"/api/admin/experiments/{experiment_id}").json()["study_flags"] == []

    unknown = client.get(
        f"/api/admin/experiments/{experiment_id}/terms/preview", params={"study_flags": "scary"}
    )
    assert unknown.status_code == 422


def test_unset_source_reports_not_ok_and_previews_the_placeholder(client: TestClient):
    assert get_settings().terms.source_url == ""
    status = client.get("/api/admin/terms").json()
    assert status["ok"] is False
    assert "TERMS__SOURCE_URL is not set" in status["error"]
    assert status["documents"] == {}

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

    # Saving needs no source read; publish is where an unreadable source bites.
    assert _flag(client, experiment_id, "sensitive")["study_flags"] == ["sensitive"]


# ── Prolific ──────────────────────────────────────────────────────────────


@respx.mock
def test_sensitive_study_sends_warning_prescreener_and_description(
    client: TestClient, enable_prolific, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _flag(client, experiment_id, "sensitive")
    create_route = _mock_prolific_study()

    _run_pilot(client, experiment_id)

    sent = json.loads(create_route.calls.last.request.content)
    assert sent["content_warnings"] == ["sensitive"]
    assert sent["content_warning_details"] == DETAILS
    assert sent["description"].startswith("<p><b>Content warning:</b> Some passages describe")
    assert "harmful-content" in _filters(sent)
    assert "deception" not in _filters(sent)


@respx.mock
def test_deception_study_sends_its_prescreener_and_no_warning(
    client: TestClient, enable_prolific, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _flag(client, experiment_id, "deception", details=None)
    create_route = _mock_prolific_study()

    _run_pilot(client, experiment_id)

    sent = json.loads(create_route.calls.last.request.content)
    assert "content_warnings" not in sent
    assert not sent["description"].startswith("<p><b>Content warning")
    assert {"filter_id": "deception", "selected_values": ["0"]} in sent["filters"]
    assert "harmful-content" not in _filters(sent)


@respx.mock
def test_both_content_flags_send_the_stronger_warning(
    client: TestClient, enable_prolific, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _flag(client, experiment_id, "sensitive", "explicit", "deception")
    create_route = _mock_prolific_study()

    _run_pilot(client, experiment_id)

    sent = json.loads(create_route.calls.last.request.content)
    assert sent["content_warnings"] == ["explicit"]
    assert {"harmful-content", "deception"} <= _filters(sent)


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
    assert not ({"harmful-content", "deception"} & _filters(sent))


@respx.mock
def test_clearing_the_flags_before_publish_clears_prolific_too(
    client: TestClient, enable_prolific, terms_source
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _flag(client, experiment_id, "sensitive", "deception")
    _mock_prolific_study()
    pilot = _run_pilot(client, experiment_id)
    update_route = respx.patch(f"{PROLIFIC_BASE}/studies/{STUDY_ID}/").mock(
        return_value=Response(200, json={"id": STUDY_ID, "status": "UNPUBLISHED"})
    )

    cleared = _flag(client, experiment_id, details="")
    assert cleared["study_flags"] == []
    assert cleared["content_warning_details"] is None
    assert _publish(client, experiment_id, pilot["id"]).status_code == 200

    sent = json.loads(update_route.calls.last.request.content)
    assert sent["content_warnings"] == []
    assert sent["content_warning_details"] is None
    assert not sent["description"].startswith("<p><b>Content warning")
    assert not ({"harmful-content", "deception"} & _filters(sent))
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["consent_statement_ref"] == "standard v1"
    assert detail["debrief_statement_ref"] is None
    rater = _start(client, experiment_id, "PID_CLEARED")
    assert rater["debrief_html"] is None


# ── pinning at first publish ──────────────────────────────────────────────


@respx.mock
def test_publish_pins_versions_and_running_studies_keep_them(
    client: TestClient, enable_prolific, terms_source: Path, sync_engine
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _flag(client, experiment_id, "deception", details=None)
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
    assert detail["debrief_statement_ref"] == "debrief v1"

    # Flags are locked with the rest of the config now.
    locked = _patch(client, experiment_id, study_flags=["sensitive"], content_warning_details="x")
    assert locked.status_code == 400
    assert "study_flags" in locked.json()["detail"]

    # A new version at the source: new experiments pick it up on their next
    # read, the published one keeps serving the pinned copy.
    (terms_source / "consent" / "standard" / "v2.md").write_text(
        "## Version two\n\nYou have {{session_length}}.\n", encoding="utf-8"
    )
    manifest = json.loads((terms_source / "manifest.json").read_text(encoding="utf-8"))
    manifest["documents"]["consent/standard"] = 2
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
    assert rater["debrief_html"] is not None

    _consent(client, rater)
    stored = _stored_consent(sync_engine, rater["rater_id"])
    assert (stored["statement"], stored["version"]) == ("standard", 1)
    assert stored["source_url"].endswith("consent/standard/v1.md")


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
        params=[("study_flags", "deception"), ("study_flags", "explicit")],
    ).json()
    assert preview["pinned"] is True
    assert preview["consent_ref"] == "standard v1"
    assert preview["debrief_ref"] is None


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
def test_publish_without_a_source_keeps_the_placeholder_for_unflagged_studies(
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
def test_publish_without_a_source_refuses_a_flagged_study(client: TestClient, enable_prolific):
    assert get_settings().terms.source_url == ""
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _flag(client, experiment_id, "deception", details=None)
    _mock_prolific_study()
    pilot = _run_pilot(client, experiment_id)

    refused = _publish(client, experiment_id, pilot["id"])
    assert refused.status_code == 400
    assert "TERMS__SOURCE_URL" in refused.json()["detail"]
    assert client.get(f"/api/admin/experiments/{experiment_id}").json()["status"] == "DRAFT"


@respx.mock
def test_failed_publish_pins_nothing_and_leaves_flags_editable(
    client: TestClient, enable_prolific, terms_source: Path
):
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    _flag(client, experiment_id, "sensitive")
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

    # Still DRAFT, so the admin can change the flags, and the retry pins
    # what the experiment says now, not what it said when the first attempt
    # failed.
    _flag(client, experiment_id, "deception", details="")
    respx.post(f"{PROLIFIC_BASE}/studies/{STUDY_ID}/transition/").mock(
        return_value=Response(200, json={"id": STUDY_ID, "status": "ACTIVE"})
    )
    assert _publish(client, experiment_id, pilot["id"]).status_code == 200
    detail = client.get(f"/api/admin/experiments/{experiment_id}").json()
    assert detail["consent_statement_ref"] == "standard v1"
    assert detail["debrief_statement_ref"] == "debrief v1"
