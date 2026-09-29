"""The #96 launch gate: what stops a study being created.

Reads the experiment row, not the dataset card. The card snapshots onto the
row at create, so the card's current state has nothing to do with what raters
will see — and #84 keeps ungrouped experiments valid, so they have no card at
all. Asking the row holds both kinds to the same bar.
"""

from __future__ import annotations

import json
import re

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


# --- One-click launch -----------------------------------------------------

_ECONOMICS = {
    "estimated_completion_time": 20,
    "reward": 450,
}


def _upload_one_question(client: TestClient, experiment_id: int) -> None:
    resp = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={
            "file": (
                "gated_n1.csv",
                "question_id,question_text,gt_answer,options,question_type\n"
                "q1,Is this useful?,Yes,Yes|No,MC\n",
                "text/csv",
            )
        },
    )
    assert resp.status_code == 200, resp.text


def _carded_experiment_with_economics(client: TestClient) -> dict:
    dataset = client.post(
        "/api/admin/datasets",
        json={"name": "gated", "waves": ["fall25"], **_CARD, **_ECONOMICS},
    ).json()
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": "gated fall25", "dataset_id": dataset["id"], "wave": "fall25"},
    ).json()
    exp = client.post("/api/admin/experiments", json={"group_id": group["id"]}).json()
    _fill_row_fields(client, exp["id"])
    _upload_one_question(client, exp["id"])
    return exp


@respx.mock
def test_one_click_creates_the_pilot_from_the_card(client: TestClient, enable_prolific):
    exp = _carded_experiment_with_economics(client)
    route = _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/launch", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["estimated_completion_time"] == 20
    assert body["reward"] == 450
    assert body["places_requested"] == 5

    sent = json.loads(route.calls[-1].request.content.decode())
    # The Prolific listing gets the blurb, not the rater guide.
    assert "Rate short passages" in sent["description"]
    # Still only a draft — publishing spends money and stays a separate action.
    assert body["prolific_study_status"] == "UNPUBLISHED"
    # The dataset has no other experiments to exclude.
    assert body["excluded_experiment_ids"] == []


def _mock_groups_by_experiment() -> None:
    """Give each experiment's participant group the id `pg-<experiment id>`."""

    def _create(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content.decode())["name"]
        exp_id = re.search(r"\bexp-(\d+)-", name).group(1)  # type: ignore[union-attr]
        return httpx.Response(200, json={"id": f"pg-{exp_id}", "name": name, "project_id": "p"})

    respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(side_effect=_create)


def _group(client: TestClient, dataset: dict, wave: str) -> dict:
    return client.post(
        "/api/admin/experiment-groups",
        json={"name": f"{dataset['name']} {wave}", "dataset_id": dataset["id"], "wave": wave},
    ).json()


def _ready_in_group(client: TestClient, group: dict, name: str) -> dict:
    exp = client.post("/api/admin/experiments", json={"group_id": group["id"], "name": name})
    assert exp.status_code == 200, exp.text
    return _fill_row_fields(client, exp.json()["id"])


@respx.mock
def test_one_click_excludes_the_datasets_other_experiments(client: TestClient, enable_prolific):
    """Every other experiment on the dataset, in any wave and any status, even
    one that never ran a study: not itself, and nothing on another dataset."""
    route = _mock_create_study()
    _mock_groups_by_experiment()
    dataset = client.post(
        "/api/admin/datasets",
        json={"name": "gated", "waves": ["fall25", "sp26"], **_CARD, **_ECONOMICS},
    ).json()
    other = client.post(
        "/api/admin/datasets", json={"name": "other", "waves": ["fall25"], **_CARD}
    ).json()
    fall25, sp26 = _group(client, dataset, "fall25"), _group(client, dataset, "sp26")

    same_wave = _ready_in_group(client, fall25, "Same wave")
    other_wave = _ready_in_group(client, sp26, "Other wave")
    never_ran = _ready_in_group(client, sp26, "Never ran")
    other_dataset = _ready_in_group(client, _group(client, other, "fall25"), "Other dataset")
    for exp in (same_wave, other_wave, other_dataset):
        pilot = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)
        assert pilot.status_code == 200, pilot.text

    target = _ready_in_group(client, fall25, "Target")
    _upload_one_question(client, target["id"])
    expected = [
        {"id": same_wave["id"], "name": "Same wave"},
        {"id": other_wave["id"], "name": "Other wave"},
        {"id": never_ran["id"], "name": "Never ran"},
    ]
    preview = client.get(f"/api/admin/experiments/{target['id']}/launch/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json() == {"excluded_experiments": expected}

    # The siblings are still DRAFT, which the pilot form would refuse as
    # exclusion targets; one-click chose them itself.
    resp = client.post(f"/api/admin/experiments/{target['id']}/launch", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["excluded_experiment_ids"] == [ref["id"] for ref in expected]

    # The sibling with no study yet still gets its group, which fills as its
    # raters arrive.
    sent = json.loads(route.calls[-1].request.content.decode())
    blocklist = next(
        entry["selected_values"]
        for entry in sent["filters"]
        if entry["filter_id"] == "participant_group_blocklist"
    )
    assert blocklist == [f"pg-{target['id']}"] + [f"pg-{ref['id']}" for ref in expected]

    # The target never lists itself, and its siblings list it.
    after = client.get(f"/api/admin/experiments/{target['id']}/launch/preview").json()
    assert after == {"excluded_experiments": expected}
    from_sibling = client.get(f"/api/admin/experiments/{never_ran['id']}/launch/preview").json()
    assert [ref["id"] for ref in from_sibling["excluded_experiments"]] == [
        same_wave["id"],
        other_wave["id"],
        target["id"],
    ]


@respx.mock
def test_a_failed_exclusion_group_is_a_prolific_error(client: TestClient, enable_prolific):
    """Excluding a sibling with no study yet creates its participant group on
    Prolific first. If Prolific refuses, the launch is the same 502 carrying
    Prolific's message as a failed study create, not a 500."""
    dataset = client.post(
        "/api/admin/datasets",
        json={"name": "gated", "waves": ["fall25"], **_CARD, **_ECONOMICS},
    ).json()
    fall25 = _group(client, dataset, "fall25")
    sibling = _ready_in_group(client, fall25, "Sibling")
    target = _ready_in_group(client, fall25, "Target")
    _upload_one_question(client, target["id"])

    def _create(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content.decode())["name"]
        if f"exp-{sibling['id']}-" in name:
            return httpx.Response(400, json={"error": {"detail": "Group limit reached"}})
        return httpx.Response(200, json={"id": "pg-own", "name": name, "project_id": "p"})

    respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(side_effect=_create)
    studies = respx.post(f"{PROLIFIC_BASE}/studies/").mock(
        return_value=httpx.Response(200, json={"id": PROLIFIC_STUDY_ID, "status": "UNPUBLISHED"})
    )

    resp = client.post(f"/api/admin/experiments/{target['id']}/launch", json={})
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == (
        "Failed to create study on Prolific. Prolific said: Group limit reached"
    )
    assert not studies.called
    assert client.get(f"/api/admin/experiments/{target['id']}/prolific/rounds").json() == []


def test_an_ungrouped_experiment_previews_no_exclusions(client: TestClient):
    exp = client.post("/api/admin/experiments", json={"name": "Scratch"}).json()
    resp = client.get(f"/api/admin/experiments/{exp['id']}/launch/preview")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"excluded_experiments": []}


@respx.mock
def test_one_click_honours_a_places_override(client: TestClient, enable_prolific):
    exp = _carded_experiment_with_economics(client)
    _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/launch", json={"places": 12})
    assert resp.status_code == 200, resp.text
    assert resp.json()["places_requested"] == 12


@respx.mock
def test_one_click_refuses_without_questions(client: TestClient, enable_prolific):
    dataset = client.post(
        "/api/admin/datasets",
        json={"name": "gated", "waves": ["fall25"], **_CARD, **_ECONOMICS},
    ).json()
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": "gated fall25", "dataset_id": dataset["id"], "wave": "fall25"},
    ).json()
    exp = client.post("/api/admin/experiments", json={"group_id": group["id"]}).json()
    _fill_row_fields(client, exp["id"])
    route = _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/launch", json={})
    assert resp.status_code == 400
    assert "upload questions" in resp.json()["detail"]
    assert route.call_count == 0


@respx.mock
def test_one_click_names_the_missing_upload_before_the_missing_text(
    client: TestClient, enable_prolific
):
    """The bare case: no upload AND no rater text, which is how a fresh
    grouped experiment now starts.

    The rater text arrives WITH the upload — the pipeline stamps it into the
    exported file — so reporting it as the blocker would misdiagnose the
    problem and send the admin in a circle: the way to supply it is to upload.
    Note this experiment deliberately does NOT call `_fill_row_fields`.
    """
    dataset = client.post(
        "/api/admin/datasets",
        json={"name": "gated", "waves": ["fall25"], **_CARD, **_ECONOMICS},
    ).json()
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": "gated fall25", "dataset_id": dataset["id"], "wave": "fall25"},
    ).json()
    exp = client.post("/api/admin/experiments", json={"group_id": group["id"]}).json()
    assert exp["launch_blockers"], "precondition: the row has no rater text yet"
    route = _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/launch", json={})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "upload questions" in detail
    assert "rater instructions" not in detail
    assert route.call_count == 0


@respx.mock
def test_one_click_refuses_a_card_without_economics_until_they_are_added(
    client: TestClient, enable_prolific
):
    """Economics are optional at onboarding, but only a complete card launches
    in one click. A dataset's first study usually goes through the pilot form,
    which asks for them; once they are on the card, the next study can skip
    the form."""
    exp = _carded_experiment(client)
    _upload_one_question(client, exp["id"])
    route = _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/launch", json={})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "complete dataset card" in detail
    assert "estimated completion time, reward" in detail
    assert "pilot form" in detail
    assert route.call_count == 0

    dataset_id = client.get(f"/api/admin/experiments/{exp['id']}").json()["group_dataset_id"]
    card = client.patch(f"/api/admin/datasets/{dataset_id}", json=_ECONOMICS).json()
    assert card["complete"] is True

    resp = client.post(f"/api/admin/experiments/{exp['id']}/launch", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reward"] == _ECONOMICS["reward"]


@respx.mock
def test_one_click_needs_the_whole_card_not_just_economics(client: TestClient, enable_prolific):
    """Complete means launch-ready plus economics. Names given explicitly at
    create make the experiment launchable, but the card itself still lacks its
    name templates, so it is not complete and one-click refuses."""
    dataset = client.post(
        "/api/admin/datasets",
        json={"name": "gated", "waves": ["fall25"], "study_blurb": "Rate.", **_ECONOMICS},
    ).json()
    assert dataset["complete"] is False
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": "gated fall25", "dataset_id": dataset["id"], "wave": "fall25"},
    ).json()
    exp = client.post(
        "/api/admin/experiments",
        json={"group_id": group["id"], "name": "Named", "internal_name": "named fall25"},
    ).json()
    _fill_row_fields(client, exp["id"])
    _upload_one_question(client, exp["id"])
    route = _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/launch", json={})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "missing external study name, internal study name." in detail
    assert "Fill them in on the dataset card" in detail
    assert route.call_count == 0


@respx.mock
def test_one_click_applies_the_launch_gate(client: TestClient, enable_prolific):
    """One-click is a shortcut through the pilot form, not around readiness.

    Questions are uploaded first so the gate is what refuses — without them the
    missing upload is reported instead, which is the check just above it.
    """
    exp = client.post("/api/admin/experiments", json={"name": "Bare"}).json()
    _upload_one_question(client, exp["id"])

    resp = client.post(f"/api/admin/experiments/{exp['id']}/launch", json={})
    assert resp.status_code == 400
    assert "rater instructions" in resp.json()["detail"]
