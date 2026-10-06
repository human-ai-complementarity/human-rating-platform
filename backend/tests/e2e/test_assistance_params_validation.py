"""Model ids in `assistance_params` are validated on create and PATCH, and the
removed `model` key is refused.

Unvalidated, a bad id fails only at rater time: `_parse_model` raises, which the
methods turn into a silent no-assistance step, or a 500 for a non-string.
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
