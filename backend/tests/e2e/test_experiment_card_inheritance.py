"""Experiments inherit their group's dataset card at create (#96).

The card is a template, not a live binding: values are copied onto the row
here, so a later card edit cannot retroactively change what raters already
saw, and the existing config lock keeps working untouched.

What the card covers is how a *study* is run. The dataset's own presentation
is not on it: the inference pipeline stamps that into the export, and the
upload applies it to the experiment.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import text

_CARD = {
    "external_study_name": "Passage comprehension rating",
    "internal_study_name": "{dataset} {wave} {method}",
    "num_ratings_per_question": 5,
}

# What the pipeline stamps into the exported file, applied at upload.
_EXPORT_META = {
    "description": "Answer using only the passage. Do not use web search.",
    "human_prompt_prefix": "Based only on the passage above:",
    "human_prompt_suffix": "Rate your confidence honestly.",
    "system_prompt": "You are an evaluator. Be conservative.",
    "prolific_pool": "uk_representative_sample",
}


def _carded_group(client: TestClient, name: str = "medqa", wave: str = "fall25") -> dict:
    dataset = client.post(
        "/api/admin/datasets", json={"name": name, "waves": [wave], **_CARD}
    ).json()
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": f"{name} {wave}", "dataset_id": dataset["id"], "wave": wave},
    ).json()
    return group


def _create(client: TestClient, **payload) -> dict:
    resp = client.post("/api/admin/experiments", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_grouped_experiment_inherits_every_card_field(client: TestClient):
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"], assistance_method="top_n")

    assert exp["num_ratings_per_question"] == 5
    # External name is the card's, verbatim; internal carries wave + arm.
    assert exp["name"] == "Passage comprehension rating"
    assert exp["internal_name"] == "medqa fall25 top_n"


def test_create_leaves_the_exported_fields_blank_for_the_upload(client: TestClient):
    """The card must not pre-fill what the export is authoritative for.

    `_apply_meta_to_experiment` never overwrites a non-empty field, and create
    is strictly before any upload — so anything seeded here would turn the
    pipeline's value into a discarded conflict. Blank is the whole mechanism.
    """
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"], assistance_method="top_n")

    for field in _EXPORT_META:
        assert exp[field] is None, field


def test_create_pins_no_model_of_its_own(client: TestClient):
    """The models belong to the wave, and the wave's copy lives in the
    pipeline. Create has no source for them and must not invent one — seeding
    anything here would meet the export's value with the never-overwrite rule
    and discard the authoritative one as a conflict."""
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"], assistance_method="top_n")

    assert exp["assistance_params"] is None


def test_an_explicit_payload_value_is_never_overridden(client: TestClient):
    group = _carded_group(client)
    exp = _create(
        client,
        name="Hand-picked name",
        group_id=group["id"],
        assistance_method="top_n",
        num_ratings_per_question=2,
        assistance_params={"assistance_models": {"top_n": "openrouter/openai/gpt-4o"}, "n": 4},
    )

    assert exp["name"] == "Hand-picked name"
    assert exp["num_ratings_per_question"] == 2
    assert exp["assistance_params"]["assistance_models"] == {"top_n": "openrouter/openai/gpt-4o"}
    assert exp["assistance_params"]["n"] == 4


def test_ungrouped_create_is_unchanged(client: TestClient):
    exp = _create(client, name="Scratch")
    assert exp["name"] == "Scratch"
    assert exp["description"] is None
    assert exp["group_id"] is None


def test_a_nameless_create_without_a_card_template_is_a_clean_400(client: TestClient):
    resp = client.post("/api/admin/experiments", json={})
    assert resp.status_code == 400
    assert "external_study_name" in resp.json()["detail"]


def test_identical_renders_are_disambiguated(client: TestClient):
    group = _carded_group(client)
    first = _create(client, group_id=group["id"], assistance_method="none")
    second = _create(client, group_id=group["id"], assistance_method="top_n")

    assert first["name"] == "Passage comprehension rating"
    assert second["name"] == "Passage comprehension rating (2)"
    # Internal names differ on the arm, so they need no disambiguation.
    assert first["internal_name"] == "medqa fall25 none"
    assert second["internal_name"] == "medqa fall25 top_n"


def _upload_with_meta(client: TestClient, experiment_id: int, meta: dict) -> dict:
    csv_data = (
        f"#META: {json.dumps(meta)}\n"
        "question_id,question_text,gt_answer,options,question_type\n"
        "q1,Is this useful?,Yes,Yes|No,MC\n"
    )
    resp = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("medqa_n1.csv", csv_data, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_exported_meta_reaches_a_carded_experiment(client: TestClient):
    """The upload is the source for what the pipeline stamps (#96).

    This is the regression the removal buys. While the platform card carried
    these too, they were already on the row by the time the file arrived, and
    `_apply_meta_to_experiment`'s never-overwrite rule reported the
    authoritative value as a conflict and dropped it on the floor.
    """
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"])

    body = _upload_with_meta(client, exp["id"], _EXPORT_META)
    assert body["meta_conflicts"] == []
    assert sorted(body["meta_applied"]) == sorted(_EXPORT_META)

    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    for field, value in _EXPORT_META.items():
        assert after[field] == value, field


def test_a_value_already_typed_on_the_experiment_still_wins(client: TestClient):
    """Never-overwrite is unchanged — only what reaches the row first moved.

    A human editing the experiment while it is DRAFT is a deliberate override,
    so the upload reports the disagreement rather than silently undoing it.
    """
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"])
    client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={"assistance_method": "none", "description": "A hand-written guide."},
    )

    body = _upload_with_meta(client, exp["id"], {"description": "A different guide."})
    assert body["meta_conflicts"] == ["description"]
    assert body["meta_applied"] == []

    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["description"] == "A hand-written guide."


def test_config_fields_are_frozen_by_the_existing_config_lock(client: TestClient, sync_engine):
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"])

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET status = 'LAUNCH' WHERE id = :id"),
            {"id": exp["id"]},
        )

    resp = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={"assistance_method": "none", "description": "Rewritten after launch"},
    )
    assert resp.status_code == 400
    assert "locked" in resp.json()["detail"].lower()


def test_duplicate_carries_the_snapshot(client: TestClient):
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"])
    _upload_with_meta(client, exp["id"], _EXPORT_META)

    dup = client.post(f"/api/admin/experiments/{exp['id']}/duplicate")
    assert dup.status_code == 200, dup.text
    body = dup.json()
    assert body["description"] == _EXPORT_META["description"]
    assert body["human_prompt_prefix"] == _EXPORT_META["human_prompt_prefix"]
    assert body["group_id"] == group["id"]
