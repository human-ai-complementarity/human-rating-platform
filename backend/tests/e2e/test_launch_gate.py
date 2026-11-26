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

# The gate's text field is no longer on the platform card (#96): the pipeline
# stamps it into the export and the upload applies it to the row. Tests here
# only need it *on the row*, so they write it directly.
_ROW_FIELDS = {"description": "Answer using only the passage."}


def _entry(model: str) -> dict:
    """A complete model entry: the upload pins these, not bare ids."""
    return {"model": model, "reasoning_effort": None, "text_verbosity": None, "temperature": 0}


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


def _with_a_question(client: TestClient, **payload) -> dict:
    """A new experiment past the "questions" blocker, so a test sees the rest."""
    exp = client.post("/api/admin/experiments", json=payload).json()
    _upload_one_question(client, exp["id"])
    return exp


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
    exp = _with_a_question(client, group_id=group["id"], assistance_method=method)
    return _fill_row_fields(client, exp["id"], method)


def test_a_fully_configured_experiment_reports_ready(client: TestClient):
    exp = _carded_experiment(client)
    assert exp["launch_ready"] is True
    assert exp["launch_blockers"] == []
    assert exp["launch_blocker_fixes"] == []


def test_a_bare_experiment_reports_what_it_is_missing(client: TestClient):
    exp = client.post("/api/admin/experiments", json={"name": "Bare"}).json()
    assert exp["launch_ready"] is False
    assert exp["launch_blockers"] == ["questions", "rater instructions", "internal study name"]
    # One fix per blocker, in the same order, so the UI renders them as served.
    assert [fix.split(":")[0] for fix in exp["launch_blocker_fixes"]] == [
        "Questions",
        "Rater instructions",
        "Internal study name",
    ]


@pytest.mark.parametrize(
    "framing",
    [{}, {"human_prompt_prefix": "Given the passage:"}, {"human_prompt_suffix": "Rate it."}],
)
def test_the_prompt_prefix_and_suffix_are_optional(client: TestClient, framing):
    """A dataset may frame its questions with one, both or neither."""
    exp = _with_a_question(client, name="Unframed")
    patched = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={
            "assistance_method": "none",
            "internal_name": "unframed internal",
            "description": "Instructions.",
            **framing,
        },
    ).json()
    assert patched["launch_blockers"] == []


def test_an_ungrouped_experiment_can_still_be_ready(client: TestClient):
    """#84 keeps ungrouped experiments valid. They have no card, but they can
    carry the same values on the row, and the gate asks the row."""
    exp = _with_a_question(client, name="Scratch")
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
    exp = _with_a_question(client, name="Control")
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
    exp = _with_a_question(client, name="Assisted", assistance_method="top_n")
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
        ({"top_n": _entry("openrouter/test/top-n-entry")}, []),
        (
            {"human_as_a_tool": _entry("openrouter/test/human-as-a-tool-entry")},
            ["assistance model"],
        ),
    ],
)
def test_an_assistance_models_entry_for_the_current_method_is_a_model(
    client: TestClient, assistance_models, blockers
):
    """The upload pins the wave's model per method; only the entry for the
    method this arm runs counts."""
    exp = _with_a_question(
        client,
        name="Mapped",
        assistance_method="top_n",
        assistance_params={"assistance_models": assistance_models},
    )
    patched = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={
            "assistance_method": "top_n",
            "internal_name": "mapped internal",
            **_ROW_FIELDS,
        },
    ).json()
    assert patched["launch_blockers"] == blockers


def test_a_provider_without_a_key_blocks_launch(client: TestClient, monkeypatch):
    """Validation accepts an id for any registered provider; the key it needs
    is checked at the gate, where the fix is a config change, rather than
    failing every rater's first call."""
    monkeypatch.setattr(get_settings().llm, "oai_api_key", "")
    exp = _with_a_question(
        client,
        name="Keyless",
        assistance_method="top_n",
        assistance_params={"assistance_models": {"top_n": _entry("oai/test/top-n-entry")}},
    )
    patched = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={"assistance_method": "top_n", "internal_name": "keyless internal", **_ROW_FIELDS},
    ).json()
    assert patched["launch_blockers"] == ["assistance model key"]
    assert "LLM__OAI_API_KEY" in patched["launch_blocker_fixes"][0]

    monkeypatch.setattr(get_settings().llm, "oai_api_key", "sk-test")
    assert client.get(f"/api/admin/experiments/{exp['id']}").json()["launch_blockers"] == []


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
    assert "Rater instructions: part of the pipeline export" in detail
    assert "Internal study name: copied from the dataset card's template only when" in detail
    # The same fixes the experiment response serves the admin UI.
    for fix in exp["launch_blocker_fixes"]:
        assert fix in detail
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
    assert "per method under assistance_models" in detail
    assert (
        "PATCH /api/admin/experiments/<id> with assistance_params.assistance_models.<method>."
        in detail
    )
    assert "assistance_params.model" not in detail
    assert route.call_count == 0


@respx.mock
def test_a_ready_experiment_launches(client: TestClient, enable_prolific):
    exp = _carded_experiment(client)
    _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)
    assert resp.status_code == 200, resp.text


# --- Same-dataset exclusions ------------------------------------------------

_ECONOMICS = {
    "estimated_completion_time": 20,
    "reward": 450,
}


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
    exp = _with_a_question(client, group_id=group["id"], name=name)
    return _fill_row_fields(client, exp["id"])


@respx.mock
def test_a_pilot_can_exclude_the_datasets_other_experiments_in_any_status(
    client: TestClient, enable_prolific
):
    route = _mock_create_study()
    _mock_groups_by_experiment()
    dataset = client.post(
        "/api/admin/datasets",
        json={"name": "gated", "waves": ["fall25", "sp26"], **_CARD},
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
    siblings = [same_wave["id"], other_wave["id"], never_ran["id"]]
    studies_before = route.call_count

    refused = client.post(
        f"/api/admin/experiments/{target['id']}/prolific/pilot",
        json={**_PILOT, "excluded_experiment_ids": [*siblings, other_dataset["id"]]},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"] == (
        "Exclusion targets must be finished experiments or on the same dataset. "
        f"Rejected: {other_dataset['id']} (DRAFT)."
    )
    assert route.call_count == studies_before

    resp = client.post(
        f"/api/admin/experiments/{target['id']}/prolific/pilot",
        json={**_PILOT, "excluded_experiment_ids": siblings},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["excluded_experiment_ids"] == siblings

    # The sibling with no study yet still gets its group, which fills as its
    # raters arrive.
    sent = json.loads(route.calls[-1].request.content.decode())
    blocklist = next(
        entry["selected_values"]
        for entry in sent["filters"]
        if entry["filter_id"] == "participant_group_blocklist"
    )
    assert blocklist == [f"pg-{target['id']}"] + [f"pg-{exp_id}" for exp_id in siblings]


@respx.mock
def test_a_draft_round_can_re_add_a_same_dataset_exclusion(client: TestClient, enable_prolific):
    _mock_create_study()
    _mock_groups_by_experiment()
    respx.patch(f"{PROLIFIC_BASE}/studies/{PROLIFIC_STUDY_ID}/").mock(
        return_value=httpx.Response(200, json={"id": PROLIFIC_STUDY_ID, "status": "UNPUBLISHED"})
    )
    dataset = client.post(
        "/api/admin/datasets", json={"name": "gated", "waves": ["fall25"], **_CARD}
    ).json()
    fall25 = _group(client, dataset, "fall25")
    sibling = _ready_in_group(client, fall25, "Sibling")
    target = _ready_in_group(client, fall25, "Target")

    pilot = client.post(f"/api/admin/experiments/{target['id']}/prolific/pilot", json=_PILOT)
    assert pilot.status_code == 200, pilot.text
    assert pilot.json()["excluded_experiment_ids"] == []

    resp = client.patch(
        f"/api/admin/experiments/{target['id']}/prolific/rounds/{pilot.json()['id']}",
        json={"excluded_experiment_ids": [sibling["id"]]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["excluded_experiment_ids"] == [sibling["id"]]


@respx.mock
def test_a_failed_exclusion_group_is_a_prolific_error(client: TestClient, enable_prolific):
    """Excluding a sibling with no study yet creates its participant group on
    Prolific first. If Prolific refuses, the launch is the same 502 carrying
    Prolific's message as a failed study create, not a 500."""
    dataset = client.post(
        "/api/admin/datasets",
        json={"name": "gated", "waves": ["fall25"], **_CARD},
    ).json()
    fall25 = _group(client, dataset, "fall25")
    sibling = _ready_in_group(client, fall25, "Sibling")
    target = _ready_in_group(client, fall25, "Target")

    def _create(request: httpx.Request) -> httpx.Response:
        name = json.loads(request.content.decode())["name"]
        if f"exp-{sibling['id']}-" in name:
            return httpx.Response(400, json={"error": {"detail": "Group limit reached"}})
        return httpx.Response(200, json={"id": "pg-own", "name": name, "project_id": "p"})

    respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(side_effect=_create)
    studies = respx.post(f"{PROLIFIC_BASE}/studies/").mock(
        return_value=httpx.Response(200, json={"id": PROLIFIC_STUDY_ID, "status": "UNPUBLISHED"})
    )

    resp = client.post(
        f"/api/admin/experiments/{target['id']}/prolific/pilot",
        json={**_PILOT, "excluded_experiment_ids": [sibling["id"]]},
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == (
        "Failed to create study on Prolific. Prolific said: Group limit reached"
    )
    assert not studies.called
    assert client.get(f"/api/admin/experiments/{target['id']}/prolific/rounds").json() == []


@respx.mock
def test_without_questions_the_pilot_is_refused(client: TestClient, enable_prolific):
    """A pilot with nothing to rate would publish the experiment into LAUNCH,
    which blocks uploads. It is refused with the blocker the API serves."""
    dataset = client.post(
        "/api/admin/datasets",
        json={"name": "gated", "waves": ["fall25"], **_CARD, **_ECONOMICS},
    ).json()
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": "gated fall25", "dataset_id": dataset["id"], "wave": "fall25"},
    ).json()
    exp = client.post("/api/admin/experiments", json={"group_id": group["id"]}).json()
    exp = _fill_row_fields(client, exp["id"])
    assert exp["launch_blockers"] == ["questions"]
    route = _mock_create_study()

    pilot = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)

    assert pilot.status_code == 400
    assert pilot.json()["detail"] == (
        "Cannot launch: this experiment is missing questions. " + exp["launch_blocker_fixes"][0]
    )
    assert "upload questions" in exp["launch_blocker_fixes"][0]
    assert route.call_count == 0

    _upload_one_question(client, exp["id"])
    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["launch_ready"] is True


@respx.mock
def test_the_pilot_names_the_missing_upload_before_the_missing_text(
    client: TestClient, enable_prolific
):
    """The bare case: no upload AND no rater text, which is how a fresh
    grouped experiment now starts.

    The rater text arrives WITH the upload — the pipeline stamps it into the
    exported file — so the upload is named first, and its fix says to upload
    before setting the text by hand rather than sending the admin in a circle.
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

    resp = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail.startswith(
        "Cannot launch: this experiment is missing questions, rater instructions."
    )
    assert detail.index("Questions: upload questions") < detail.index("Rater instructions:")
    assert "upload before setting those by hand" in detail
    assert route.call_count == 0


# --- Testing previews (#174) ---------------------------------------------
# The pipeline's `create_human_rating_study.py --mode testing` makes preview-only
# drafts tagged `preview-testing`, holding a subsample of the real questions.
# They sit in the dataset's real group, so they look launchable; the tag is
# what keeps every Prolific write away from them.

_TESTING_FIX_START = "Testing preview only: this experiment is tagged preview-testing"


def _tag(client: TestClient, experiment_id: int, tags: list[str]) -> dict:
    # An update restates the method (ExperimentUpdate requires it).
    method = client.get(f"/api/admin/experiments/{experiment_id}").json()["assistance_method"]
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}",
        json={"assistance_method": method, "tags": tags},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _testing_preview(client: TestClient) -> dict:
    """An otherwise launch-ready experiment on a complete card, tagged as the
    pipeline's testing mode tags it."""
    exp = _carded_experiment_with_economics(client)
    return _tag(client, exp["id"], ["preview-testing", "wave-check"])


def test_a_testing_preview_reports_the_blocker_first(client: TestClient):
    exp = _testing_preview(client)
    assert exp["launch_ready"] is False
    assert exp["launch_blockers"] == ["testing preview"]
    assert exp["launch_blocker_fixes"][0].startswith(_TESTING_FIX_START)
    assert "--mode prod" in exp["launch_blocker_fixes"][0]
    assert "remove the preview-testing tag" in exp["launch_blocker_fixes"][0]

    # Ahead of anything missing: filling those in would not make it launchable.
    bare = client.post(
        "/api/admin/experiments", json={"name": "Bare", "tags": ["preview-testing"]}
    ).json()
    assert bare["launch_blockers"][:2] == ["testing preview", "questions"]

    # Taking the tag off is the way out.
    assert _tag(client, exp["id"], ["wave-check"])["launch_ready"] is True


def test_the_reserved_tag_matches_in_any_case(client: TestClient):
    """Tag names are case-insensitive everywhere else, so the reserved one is too."""
    exp = _carded_experiment_with_economics(client)
    assert _tag(client, exp["id"], ["Preview-Testing"])["launch_blockers"] == ["testing preview"]


def test_the_summary_is_served_as_the_400_words_it(client: TestClient):
    """The UI shows the summary as served rather than wording its own."""
    exp = _testing_preview(client)
    assert exp["launch_blocker_summary"] == "this experiment is a testing preview"
    bare = client.post(
        "/api/admin/experiments", json={"name": "Bare", "tags": ["preview-testing"]}
    ).json()
    assert bare["launch_blocker_summary"] == (
        "this experiment is a testing preview and is missing questions, rater instructions, "
        "internal study name"
    )
    # Ready once the tag is off: no summary at all.
    assert _tag(client, exp["id"], [])["launch_blocker_summary"] is None


@respx.mock
def test_a_testing_preview_cannot_pilot(client: TestClient, enable_prolific):
    exp = _testing_preview(client)
    route = _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)

    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Cannot launch: this experiment is a testing preview. " + exp["launch_blocker_fixes"][0]
    )
    assert route.call_count == 0


@respx.mock
def test_a_testing_preview_names_what_it_is_missing_too(client: TestClient, enable_prolific):
    exp = client.post(
        "/api/admin/experiments", json={"name": "Bare", "tags": ["preview-testing"]}
    ).json()
    _mock_create_study()

    resp = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)

    assert resp.status_code == 400
    assert resp.json()["detail"].startswith(
        "Cannot launch: this experiment is a testing preview and is missing questions, "
        "rater instructions, internal study name. Testing preview only:"
    )


def _pilot_then_tag(client: TestClient) -> tuple[int, dict, respx.Route]:
    """A pilot draft made before the tag went on, then the tag added: the
    gate at pilot time no longer covers it. Returns the experiment id, the
    pilot round and the mocked study-create route."""
    exp = _carded_experiment_with_economics(client)
    route = _mock_create_study()
    pilot = client.post(f"/api/admin/experiments/{exp['id']}/prolific/pilot", json=_PILOT)
    assert pilot.status_code == 200, pilot.text
    _tag(client, exp["id"], ["preview-testing"])
    return exp["id"], pilot.json(), route


@respx.mock
def test_a_testing_preview_cannot_publish_a_round(client: TestClient, enable_prolific):
    exp_id, pilot, _ = _pilot_then_tag(client)
    transition = respx.post(f"{PROLIFIC_BASE}/studies/{PROLIFIC_STUDY_ID}/transition/").mock(
        return_value=httpx.Response(200, json={"id": PROLIFIC_STUDY_ID, "status": "ACTIVE"})
    )

    resp = client.post(f"/api/admin/experiments/{exp_id}/prolific/rounds/{pilot['id']}/publish")

    assert resp.status_code == 400
    assert resp.json()["detail"].startswith(
        "Cannot publish: this experiment is a testing preview. " + _TESTING_FIX_START
    )
    assert transition.call_count == 0
    rounds = client.get(f"/api/admin/experiments/{exp_id}/prolific/rounds")
    assert [r["prolific_study_status"] for r in rounds.json()] == ["UNPUBLISHED"]


@respx.mock
def test_a_testing_preview_cannot_launch_a_round(client: TestClient, enable_prolific):
    exp_id, _, route = _pilot_then_tag(client)
    calls_before = route.call_count

    resp = client.post(f"/api/admin/experiments/{exp_id}/prolific/rounds", json={"places": 10})

    assert resp.status_code == 400
    # Refused for the tag, before the round checks would have their say.
    assert resp.json()["detail"].startswith(
        "Cannot launch a new round: this experiment is a testing preview. " + _TESTING_FIX_START
    )
    assert route.call_count == calls_before
