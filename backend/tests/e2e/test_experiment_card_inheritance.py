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
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from services.assistance.base import InteractionStep, StepType
from services.assistance.methods.human_as_a_tool import HumanAsAToolMethod

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


def test_export_carries_the_assistance_session_id(client: TestClient):
    """Per Joshua on #96: don't duplicate — attach the id and join for the
    rest. Blank rather than absent for unassisted ratings, so the CSV shape
    stays constant across experiments."""
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"])
    client.post(
        f"/api/admin/experiments/{exp['id']}/upload",
        files={
            "file": (
                "medqa_n1.csv",
                "question_id,question_text,gt_answer,options,question_type\n"
                "q1,Is this useful?,Yes,Yes|No,MC\n",
                "text/csv",
            )
        },
    )

    with client.stream("GET", f"/api/admin/experiments/{exp['id']}/export") as response:
        assert response.status_code == 200
        header = "".join(response.iter_text()).splitlines()[0]

    assert header.endswith("counts_toward_target,assistance_session_id")


_LEGACY_PARAMS = {"assistance_models": {"top_n": "gpt-4o"}, "n": 3}


def _store_legacy_params(sync_engine, experiment_id: int, status: str = "DRAFT") -> None:
    """Write params the API now refuses, as rows from before validation hold."""
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE experiments SET assistance_params = :params, status = :status WHERE id = :id"
            ),
            {"params": json.dumps(_LEGACY_PARAMS), "status": status, "id": experiment_id},
        )


@pytest.mark.parametrize("status", ["DRAFT", "LAUNCH"])
def test_a_restated_legacy_model_does_not_block_a_rename(
    client: TestClient, sync_engine, status: str
):
    """The admin UI's name and instructions saves re-send the stored
    `assistance_params` in full. Validating a model the PATCH merely restates
    would block those edits, and once launched the config lock stops the model
    being fixed to unblock them."""
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"], assistance_method="top_n")
    _store_legacy_params(sync_engine, exp["id"], status)

    stored = client.get(f"/api/admin/experiments/{exp['id']}").json()
    resp = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={
            "assistance_method": stored["assistance_method"],
            "assistance_params": stored["assistance_params"],
            "name": "Renamed",
        },
    )
    assert resp.status_code == 200, resp.text
    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["name"] == "Renamed"
    assert after["assistance_params"] == _LEGACY_PARAMS


@pytest.mark.parametrize("key", ["confidence_model", "clustering_model"])
def test_the_instrument_overrides_are_validated_too(client: TestClient, sync_engine, key: str):
    """A bad confidence or clustering model also degrades to no assistance."""
    body = {"name": "Instrument", "assistance_method": "human_as_a_tool"}
    created = client.post(
        "/api/admin/experiments", json={**body, "assistance_params": {key: "gpt-4o"}}
    )
    assert created.status_code == 400
    assert f"assistance_params.{key}" in created.json()["detail"]

    exp = _create(client, **body)
    legacy = {key: "gpt-4o"}
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET assistance_params = :params WHERE id = :id"),
            {"params": json.dumps(legacy), "id": exp["id"]},
        )
    url = f"/api/admin/experiments/{exp['id']}"
    method = {"assistance_method": "human_as_a_tool"}

    restated = client.patch(url, json={**method, "assistance_params": legacy, "name": "Renamed"})
    assert restated.status_code == 200, restated.text
    changed = client.patch(url, json={**method, "assistance_params": {key: "claude-sonnet-4-6"}})
    assert changed.status_code == 400
    assert client.get(url).json()["assistance_params"] == legacy


# Sentinel map entries, distinct from every settings default.
_MAP_TOP_N = "openrouter/test/top-n-entry"
_MAP_HAAT = "openrouter/test/human-as-a-tool-entry"


def _first_question(client: TestClient, experiment_id: int, pid: str) -> tuple[dict, dict]:
    """A new rater's session headers, and the first question they are served."""
    session = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment_id,
            "PROLIFIC_PID": pid,
            "STUDY_ID": "STUDY_1",
            "SESSION_ID": f"SESSION_{pid}",
        },
    ).json()
    headers = {"X-Rater-Session": session["rater_session_token"]}
    return headers, client.get("/api/raters/next-question", headers=headers).json()


def _session_params(sync_engine) -> dict:
    with sync_engine.connect() as conn:
        params = conn.execute(text("SELECT params FROM assistance_sessions")).scalar_one()
    return json.loads(params)


def test_the_session_records_a_model_resolved_from_the_map(
    client: TestClient, sync_engine, monkeypatch
):
    """The map's entry, not the settings default, is what ran and is recorded."""
    monkeypatch.setattr(
        "services.assistance.methods.top_n._complete_with_schema_fallback",
        AsyncMock(side_effect=RuntimeError("no LLM in tests")),
    )
    params = {"assistance_models": {"top_n": _MAP_TOP_N}}
    exp = _create(client, name="Recorded", assistance_method="top_n", assistance_params=params)
    _upload_with_meta(client, exp["id"], {})
    headers, question = _first_question(client, exp["id"], "PID_MAP")

    resp = client.post(
        "/api/raters/assistance/start", json={"question_id": question["id"]}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert _session_params(sync_engine) == {**params, "resolved_model": _MAP_TOP_N}


def test_a_retried_session_still_records_the_model_and_logs_what_went_in(
    client: TestClient, sync_engine, monkeypatch
):
    """A failed start leaves a NONE session that the next start retries on the
    same row. The retry keeps the `resolved_model` record, while each event
    logs the params start() was actually given, without it."""
    monkeypatch.setattr(
        "services.assistance.methods.top_n._complete_with_schema_fallback",
        AsyncMock(side_effect=RuntimeError("no LLM in tests")),
    )
    params = {"assistance_models": {"top_n": _MAP_TOP_N}}
    exp = _create(client, name="Retried", assistance_method="top_n", assistance_params=params)
    _upload_with_meta(client, exp["id"], {})
    headers, question = _first_question(client, exp["id"], "PID_RETRY")

    for _ in range(2):
        resp = client.post(
            "/api/raters/assistance/start", json={"question_id": question["id"]}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["type"] == "none"

    assert _session_params(sync_engine) == {**params, "resolved_model": _MAP_TOP_N}
    with sync_engine.connect() as conn:
        events = conn.execute(text("SELECT payload FROM assistance_events ORDER BY id")).scalars()
        requests = [json.loads(payload)["request"] for payload in events]
    assert requests == [
        {"params": params, "retried_step_type": None},
        {"params": params, "retried_step_type": "none"},
    ]


def test_advance_gets_a_snapshot_without_the_removed_model_key(
    client: TestClient, sync_engine, monkeypatch
):
    """advance() runs on the session's params snapshot. What ran is recorded as
    `resolved_model`, so the snapshot never carries the removed `model` key
    that create, PATCH and upload reject, and advancing leaves it as it was."""
    ask = InteractionStep(type=StepType.ASK_INPUT, payload={"subtasks": []})
    advance = AsyncMock(return_value=InteractionStep(type=StepType.COMPLETE, is_terminal=True))
    monkeypatch.setattr(HumanAsAToolMethod, "start", AsyncMock(return_value=ask))
    monkeypatch.setattr(HumanAsAToolMethod, "advance", advance)
    params = {"assistance_models": {"human_as_a_tool": _MAP_HAAT}}
    exp = _create(
        client, name="Advanced", assistance_method="human_as_a_tool", assistance_params=params
    )
    _upload_with_meta(client, exp["id"], {})
    headers, question = _first_question(client, exp["id"], "PID_ADVANCE")

    started = client.post(
        "/api/raters/assistance/start", json={"question_id": question["id"]}, headers=headers
    )
    assert started.status_code == 200, started.text
    resp = client.post(
        "/api/raters/assistance/advance",
        json={"session_id": started.json()["session_id"], "human_input": "{}"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    snapshot = {**params, "resolved_model": _MAP_HAAT}
    assert advance.call_args.args[2] == snapshot
    assert _session_params(sync_engine) == snapshot


# --- The wave's assistance models arrive with the export too (#96) -------
#
# Same road as the prose above, for the same reason: the models are a
# property of the *wave*, their authoritative copy is the pipeline's
# `configs/waves/<wave>.yaml`, and the export script — which already builds
# the file we ingest — stamps them into `dataset_meta`. They land in
# `assistance_params["assistance_models"]` rather than columns of their own,
# so they inherit the config lock, the per-`AssistanceSession` snapshot and
# `resolve_model` for free. The map's own upload rules are covered in
# test_upload_assistance_models.py.

_UPLOADED_MODELS = {"top_n": "openrouter/anthropic/claude-sonnet-4.6"}


def _pinned_models(client: TestClient, experiment_id: int) -> dict | None:
    params = client.get(f"/api/admin/experiments/{experiment_id}").json()["assistance_params"]
    return (params or {}).get("assistance_models")


def test_the_models_ride_alongside_the_prose_in_one_upload(client: TestClient):
    """One file, one meta blob: the column-backed five and the models are
    applied together and reported in one `meta_applied`."""
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"], assistance_method="top_n")

    meta = {**_EXPORT_META, "assistance_models": _UPLOADED_MODELS}
    body = _upload_with_meta(client, exp["id"], meta)
    assert sorted(body["meta_applied"]) == sorted([*_EXPORT_META, "assistance_models.top_n"])

    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["description"] == _EXPORT_META["description"]
    assert _pinned_models(client, exp["id"]) == _UPLOADED_MODELS


def test_an_upload_declaring_no_models_leaves_unset_params_unset(client: TestClient):
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"], assistance_method="top_n")

    body = _upload_with_meta(client, exp["id"], {"description": _EXPORT_META["description"]})
    assert body["meta_applied"] == ["description"]
    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["assistance_params"] is None


def test_an_upload_declaring_no_models_leaves_pinned_ones_alone(client: TestClient):
    group = _carded_group(client)
    exp = _create(
        client,
        group_id=group["id"],
        assistance_method="top_n",
        assistance_params={"assistance_models": _UPLOADED_MODELS},
    )

    _upload_with_meta(client, exp["id"], {"description": _EXPORT_META["description"]})
    assert _pinned_models(client, exp["id"]) == _UPLOADED_MODELS


def test_one_upload_takes_an_assisted_arm_from_blocked_to_launchable(client: TestClient):
    """End to end. The gate reads the experiment *row* — rater text plus a
    pinned model — and with the models on the export, one upload now
    satisfies every part of it that the pipeline owns."""
    group = _carded_group(client)
    exp = _create(client, group_id=group["id"], assistance_method="top_n")
    assert "assistance model" in exp["launch_blockers"]

    _upload_with_meta(client, exp["id"], {**_EXPORT_META, "assistance_models": _UPLOADED_MODELS})

    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["launch_blockers"] == []
    assert after["launch_ready"] is True
