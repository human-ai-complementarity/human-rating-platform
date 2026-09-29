"""The #96 launch gate: what stops a study being created.

Reads the experiment row, not the dataset card. The card snapshots onto the
row at create, so the card's current state has nothing to do with what raters
will see — and #84 keeps ungrouped experiments valid, so they have no card at
all. Asking the row holds both kinds to the same bar.
"""

from __future__ import annotations


import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from config import get_settings

PROLIFIC_BASE = "https://api.prolific.com/api/v1"
PROLIFIC_STUDY_ID = "study-gate-1"

_CARD = {
    "external_study_name": "Passage rating",
    "internal_study_name": "{dataset} {wave} {method}",
    "study_blurb": "Rate short passages.",
}

# The gate's text fields are no longer on the platform card (#96): the
# pipeline stamps them into the export and the upload applies them to the
# row. Tests here only need them *on the row*, so they write them directly.
_ROW_FIELDS = {
    "description": "Answer using only the passage.",
    "human_prompt_prefix": "Given the passage:",
    "human_prompt_suffix": "Rate your confidence.",
}

_PILOT = {
    "description": "Test study",
    "estimated_completion_time": 10,
    "reward": 500,
    "pilot_places": 5,
    "device_compatibility": ["desktop"],
}


@pytest.fixture
def enable_prolific():
    """Enable Prolific on the cached settings, restoring them afterwards.

    Mirrors the fixture in test_characterization.py; project_id is set because
    participant-group creation requires it and the fixture would otherwise pass
    only where the dev .env happens to provide one.
    """
    settings = get_settings()
    original = (settings.prolific.api_token, settings.prolific.project_id)
    settings.prolific.api_token = "test-token"
    settings.prolific.project_id = "test-project"
    try:
        yield settings
    finally:
        settings.prolific.api_token, settings.prolific.project_id = original


def _mock_create_study() -> respx.Route:
    # Setting project_id enables the lazy participant group, which the first
    # round creates before the study; mock it so the test stays hermetic.
    respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(
        return_value=httpx.Response(200, json={"id": "pg-1", "name": "pg", "project_id": "p"})
    )
    return respx.post(f"{PROLIFIC_BASE}/studies/").mock(
        return_value=httpx.Response(200, json={"id": PROLIFIC_STUDY_ID, "status": "UNPUBLISHED"})
    )


def _fill_row_fields(client: TestClient, experiment_id: int, method: str = "none") -> dict:
    """Put the gate's text fields on the row, as an upload's `#META:` would."""
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}",
        json={"assistance_method": method, **_ROW_FIELDS},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _carded_experiment(client: TestClient, method: str = "none") -> dict:
    dataset = client.post(
        "/api/admin/datasets", json={"name": "gated", "waves": ["fall25"], **_CARD}
    ).json()
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": "gated fall25", "dataset_id": dataset["id"], "wave": "fall25"},
    ).json()
    resp = client.post(
        "/api/admin/experiments",
        json={"group_id": group["id"], "assistance_method": method},
    )
    assert resp.status_code == 200, resp.text
    return _fill_row_fields(client, resp.json()["id"], method)


def test_a_fully_configured_experiment_reports_ready(client: TestClient):
    exp = _carded_experiment(client)
    assert exp["launch_ready"] is True
    assert exp["launch_blockers"] == []


def test_a_bare_experiment_reports_what_it_is_missing(client: TestClient):
    exp = client.post("/api/admin/experiments", json={"name": "Bare"}).json()
    assert exp["launch_ready"] is False
    assert exp["launch_blockers"] == [
        "rater instructions",
        "prompt prefix",
        "prompt suffix",
        "internal study name",
    ]


def test_an_ungrouped_experiment_can_still_be_ready(client: TestClient):
    """#84 keeps ungrouped experiments valid. They have no card, but they can
    carry the same values on the row, and the gate asks the row."""
    exp = client.post("/api/admin/experiments", json={"name": "Scratch"}).json()
    patched = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={
            "assistance_method": "none",
            "internal_name": "scratch internal",
            "description": "Instructions.",
            "human_prompt_prefix": "Prefix:",
            "human_prompt_suffix": "Suffix.",
        },
    ).json()
    assert patched["launch_ready"] is True


def test_a_control_arm_needs_no_model(client: TestClient):
    """assistance_method "none" never calls an LLM, so requiring a pinned
    model for it would block every control arm."""
    exp = client.post("/api/admin/experiments", json={"name": "Control"}).json()
    patched = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={
            "assistance_method": "none",
            "internal_name": "control internal",
            "description": "Instructions.",
            "human_prompt_prefix": "Prefix:",
            "human_prompt_suffix": "Suffix.",
        },
    ).json()
    assert patched["launch_blockers"] == []


def test_an_assisted_arm_without_a_model_is_blocked(client: TestClient):
    exp = client.post(
        "/api/admin/experiments", json={"name": "Assisted", "assistance_method": "top_n"}
    ).json()
    patched = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={
            "assistance_method": "top_n",
            "internal_name": "assisted internal",
            "description": "Instructions.",
            "human_prompt_prefix": "Prefix:",
            "human_prompt_suffix": "Suffix.",
        },
    ).json()
    assert patched["launch_blockers"] == ["assistance model"]


@pytest.mark.parametrize(
    ("assistance_models", "blockers"),
    [
        ({"top_n": "openrouter/test/top-n-entry"}, []),
        ({"human_as_a_tool": "openrouter/test/human-as-a-tool-entry"}, ["assistance model"]),
    ],
)
def test_an_assistance_models_entry_for_the_current_method_is_a_model(
    client: TestClient, assistance_models, blockers
):
    """The upload pins the wave's model per method; only the entry for the
    method this arm runs counts."""
    exp = client.post(
        "/api/admin/experiments",
        json={
            "name": "Mapped",
            "assistance_method": "top_n",
            "assistance_params": {"assistance_models": assistance_models},
        },
    ).json()
    patched = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={
            "assistance_method": "top_n",
            "internal_name": "mapped internal",
            **_ROW_FIELDS,
        },
    ).json()
    assert patched["launch_blockers"] == blockers


@respx.mock
def test_pilot_is_refused_with_the_blocker_list(client: TestClient, enable_prolific):
    exp = client.post("/api/admin/experiments", json={"name": "Bare"}).json()
    route = _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "rater instructions" in detail
    # Each blocker gets the fix for where its value comes from: the text from
    # the upload's metadata, the internal name from the card only at create.
    assert "Rater instructions, prompt prefix, prompt suffix: part of the pipeline export" in detail
    assert "Internal study name: copied from the dataset card's template only when" in detail
    # Refused before anything is created on Prolific — no orphan study to clean up.
    assert route.call_count == 0


@respx.mock
def test_a_missing_model_is_refused_with_both_ways_to_supply_it(
    client: TestClient, enable_prolific
):
    exp = _carded_experiment(client, method="top_n")
    route = _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "missing assistance model." in detail
    # The export carries a per-method map; a PATCH can set an entry.
    assert "per method under `assistance_models`" in detail
    assert "PATCH assistance_params.assistance_models.<method>." in detail
    assert "assistance_params.model" not in detail
    assert route.call_count == 0


@respx.mock
def test_a_ready_experiment_launches(client: TestClient, enable_prolific):
    exp = _carded_experiment(client)
    _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)
    assert resp.status_code == 200, resp.text
