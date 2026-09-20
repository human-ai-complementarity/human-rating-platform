"""`model` as a dataset_meta key.

The inference pipeline's export stamps the wave's assistance model into the
file it already ships to the platform (inference-pipeline#171), so the model
reaches an experiment the same way its rater-facing prose does. Unlike the
other meta keys it has no Experiment column: it belongs in
`assistance_params`, where it inherits the config lock, the per-session
snapshot and `params.get("model")` precedence.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

_MODEL = "openrouter/anthropic/claude-sonnet-4.6"


def _experiment(client: TestClient, **payload) -> dict:
    payload.setdefault("name", "Model meta")
    resp = client.post("/api/admin/experiments", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload(client: TestClient, experiment_id: int, meta: dict | None) -> dict:
    head = f"#META: {json.dumps(meta)}\n" if meta is not None else ""
    csv_data = (
        f"{head}question_id,question_text,gt_answer,options,question_type\n"
        "q1,Is this useful?,Yes,Yes|No,MC\n"
    )
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("medqa_n1.csv", csv_data, "text/csv")},
    )


def test_an_uploaded_model_is_pinned(client: TestClient):
    exp = _experiment(client, assistance_method="top_n")
    resp = _upload(client, exp["id"], {"model": _MODEL})
    assert resp.status_code == 200, resp.text
    assert "model" in resp.json()["meta_applied"]

    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["assistance_params"]["model"] == _MODEL


def test_it_merges_into_assistance_params_rather_than_replacing(client: TestClient):
    """`n` and `confidence_method` share this column. Replacing it would leave
    an experiment that still reads as configured while Top-N ran on defaults."""
    exp = _experiment(client, assistance_method="top_n", assistance_params={"n": 7})
    _upload(client, exp["id"], {"model": _MODEL})

    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["assistance_params"] == {"n": 7, "model": _MODEL}


def test_a_model_already_pinned_is_reported_not_clobbered(client: TestClient):
    """An admin who typed a model in is usually running a deliberate deviation;
    snapping it back to the wave's would invalidate the comparison silently."""
    exp = _experiment(
        client,
        assistance_method="top_n",
        assistance_params={"model": "openrouter/openai/gpt-4o"},
    )
    body = _upload(client, exp["id"], {"model": _MODEL}).json()

    assert "model" in body["meta_conflicts"]
    assert "model" not in body["meta_applied"]
    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["assistance_params"]["model"] == "openrouter/openai/gpt-4o"


def test_a_deliberately_cleared_model_is_not_repinned(client: TestClient):
    """An explicit null is a choice, not an absence."""
    exp = _experiment(client, assistance_method="top_n")
    client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={"assistance_method": "top_n", "assistance_params": {"n": 3, "model": None}},
    )

    body = _upload(client, exp["id"], {"model": _MODEL}).json()
    assert "model" in body["meta_conflicts"]
    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["assistance_params"]["model"] is None


def test_a_model_the_transport_cannot_parse_rejects_the_upload(client: TestClient):
    """Storing it would be worse than rejecting it: `_parse_model` raises and
    both assistance methods swallow that into a NONE step, so the study would
    look completed and have assisted nobody."""
    exp = _experiment(client, assistance_method="top_n")
    resp = _upload(client, exp["id"], {"model": "claude-sonnet-4-6"})

    assert resp.status_code == 400
    assert "openrouter/" in resp.json()["detail"]
    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["assistance_params"] is None


def test_an_upload_without_a_model_changes_nothing(client: TestClient):
    exp = _experiment(client, assistance_method="top_n", assistance_params={"n": 4})
    _upload(client, exp["id"], {"description": "A guide."})

    after = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert after["assistance_params"] == {"n": 4}
    assert after["description"] == "A guide."


def test_the_five_column_backed_fields_are_unaffected(client: TestClient):
    exp = _experiment(client)
    body = _upload(
        client,
        exp["id"],
        {
            "description": "A guide.",
            "system_prompt": "Be conservative.",
            "human_prompt_prefix": "Given the passage:",
            "human_prompt_suffix": "Rate your confidence.",
            "prolific_pool": "uk_representative_sample",
        },
    ).json()
    assert sorted(body["meta_applied"]) == [
        "description",
        "human_prompt_prefix",
        "human_prompt_suffix",
        "prolific_pool",
        "system_prompt",
    ]


def test_an_unknown_meta_key_still_400s(client: TestClient):
    exp = _experiment(client)
    resp = _upload(client, exp["id"], {"not_a_field": "x"})
    assert resp.status_code == 400
    assert "not_a_field" in resp.json()["detail"]
