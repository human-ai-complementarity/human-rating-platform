"""Model ids and `num_samples` in `assistance_params` are validated on create
and PATCH, and the removed `model` key is refused.

Unvalidated, a bad id fails only at rater time: `_parse_model` raises, which the
methods turn into a silent no-assistance step, or a 500 for a non-string. An
unbounded `num_samples` is that many LLM calls per subtask per round.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

_GOOD = "openrouter/anthropic/claude-sonnet-4.6"


def _create(client: TestClient, params: dict | None = None):
    payload = {"name": "Params validation", "assistance_method": "top_n"}
    if params is not None:
        payload["assistance_params"] = params
    return client.post("/api/admin/experiments", json=payload)


def _patch(client: TestClient, experiment_id: int, params: dict, **extra):
    return client.patch(
        f"/api/admin/experiments/{experiment_id}",
        json={"assistance_method": "top_n", "assistance_params": params, **extra},
    )


def _params(client: TestClient, experiment_id: int) -> dict | None:
    return client.get(f"/api/admin/experiments/{experiment_id}").json()["assistance_params"]


_BAD = [
    pytest.param({"model": _GOOD}, "use 'assistance_models'", id="removed-model"),
    pytest.param({"model": None}, "use 'assistance_models'", id="removed-model-null"),
    pytest.param(
        {"assistance_models": {"top_n": "gpt-4o"}},
        "assistance_params.assistance_models.top_n",
        id="unprefixed-entry",
    ),
    pytest.param(
        {"assistance_models": {"top_n": 5}},
        "assistance_params.assistance_models.top_n",
        id="non-string-entry",
    ),
    pytest.param(
        {"assistance_models": {"decomposition": _GOOD}}, "Unknown assistance method", id="unknown"
    ),
    pytest.param({"assistance_models": "x"}, "must be an object", id="non-object-map"),
]


@pytest.mark.parametrize(("params", "expected"), _BAD)
def test_create_rejects_a_bad_model(client: TestClient, params, expected):
    resp = _create(client, params)
    assert resp.status_code == 400
    assert expected in resp.json()["detail"]


@pytest.mark.parametrize(("params", "expected"), _BAD)
def test_patch_rejects_a_bad_model_and_stores_nothing(client: TestClient, params, expected):
    exp = _create(client, {"n": 3}).json()
    resp = _patch(client, exp["id"], params)
    assert resp.status_code == 400
    assert expected in resp.json()["detail"]
    assert _params(client, exp["id"]) == {"n": 3}


def test_the_removed_model_key_hints_at_reloading_a_stale_page(client: TestClient):
    """A page opened before the update re-sends `model` on every save."""
    created = _create(client, {"model": _GOOD})
    patched = _patch(client, _create(client).json()["id"], {"model": _GOOD})
    for resp in (created, patched):
        assert resp.status_code == 400
        assert "use 'assistance_models'" in resp.json()["detail"]
        assert "reload it" in resp.json()["detail"]


def test_good_models_and_clears_pass(client: TestClient):
    exp = _create(client, {"assistance_models": {"top_n": _GOOD}}).json()
    assert _patch(client, exp["id"], {"assistance_models": {"top_n": None}}).status_code == 200
    assert _patch(client, exp["id"], {"assistance_models": None}).status_code == 200


def test_a_restated_legacy_value_does_not_block_an_unrelated_edit(client: TestClient, sync_engine):
    """The UI re-sends the stored params on every save; a value written before
    validation existed must not lock the experiment out of edits."""
    exp = _create(client).json()
    legacy = {"assistance_models": {"top_n": "gpt-4o"}}
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET assistance_params = :p WHERE id = :id"),
            {"p": json.dumps(legacy), "id": exp["id"]},
        )

    renamed = _patch(client, exp["id"], legacy, name="Renamed")
    assert renamed.status_code == 200, renamed.text

    changed = _patch(client, exp["id"], {"assistance_models": {"top_n": "gpt-4.1"}})
    assert changed.status_code == 400
    assert _params(client, exp["id"]) == legacy


_NUM_SAMPLES_RANGE = "assistance_params.num_samples must be an integer from 1 to 10"


@pytest.mark.parametrize("num_samples", [1, 10])
def test_num_samples_in_range_passes(client: TestClient, num_samples):
    created = _create(client, {"num_samples": num_samples})
    assert created.status_code == 200, created.text
    assert _params(client, created.json()["id"]) == {"num_samples": num_samples}

    exp = _create(client, {"n": 3}).json()
    patched = _patch(client, exp["id"], {"num_samples": num_samples})
    assert patched.status_code == 200, patched.text
    assert _params(client, exp["id"]) == {"n": 3, "num_samples": num_samples}


_BAD_NUM_SAMPLES = [0, 11, -1, 2.5, "5", True]


@pytest.mark.parametrize("num_samples", _BAD_NUM_SAMPLES)
def test_create_rejects_num_samples_out_of_range(client: TestClient, num_samples):
    resp = _create(client, {"num_samples": num_samples})
    assert resp.status_code == 400
    assert _NUM_SAMPLES_RANGE in resp.json()["detail"]


@pytest.mark.parametrize("num_samples", _BAD_NUM_SAMPLES)
def test_patch_rejects_num_samples_out_of_range_and_stores_nothing(client: TestClient, num_samples):
    """Over a stored 1, so `True`, which equals 1, is refused too."""
    exp = _create(client, {"num_samples": 1}).json()
    resp = _patch(client, exp["id"], {"num_samples": num_samples})
    assert resp.status_code == 400
    assert _NUM_SAMPLES_RANGE in resp.json()["detail"]
    assert _params(client, exp["id"]) == {"num_samples": 1}


def test_a_restated_legacy_num_samples_does_not_block_an_unrelated_edit(
    client: TestClient, sync_engine
):
    exp = _create(client).json()
    legacy = {"num_samples": 50}
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET assistance_params = :p WHERE id = :id"),
            {"p": json.dumps(legacy), "id": exp["id"]},
        )

    renamed = _patch(client, exp["id"], legacy, name="Renamed")
    assert renamed.status_code == 200, renamed.text
    partial = _patch(client, exp["id"], {"n": 4})
    assert partial.status_code == 200, partial.text

    changed = _patch(client, exp["id"], {"num_samples": 60})
    assert changed.status_code == 400
    assert _params(client, exp["id"]) == {"num_samples": 50, "n": 4}


def test_a_null_num_samples_clears_it_to_the_default(client: TestClient):
    """Stored like a cleared model; the method reads it as the default."""
    created = _create(client, {"num_samples": None})
    assert created.status_code == 200, created.text
    assert _params(client, created.json()["id"]) == {"num_samples": None}

    exp = _create(client, {"num_samples": 7}).json()
    cleared = _patch(client, exp["id"], {"num_samples": None})
    assert cleared.status_code == 200, cleared.text
    assert _params(client, exp["id"]) == {"num_samples": None}
