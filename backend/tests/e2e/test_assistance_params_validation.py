"""Model entries in `assistance_params` are validated on create and PATCH, and
the removed `model` key is refused.

Unvalidated, a bad entry fails only at rater time: `parse_model` raises or the
provider rejects an option, which the methods turn into a silent no-assistance
step.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

_GOOD = {
    "model": "openrouter/anthropic/claude-sonnet-4.6",
    "reasoning_effort": None,
    "text_verbosity": None,
    "temperature": 0,
}
_OPENAI = {
    "model": "openai/gpt-5.6-luna",
    "reasoning_effort": "low",
    "text_verbosity": "low",
    "temperature": None,
}


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


def _entry(**overrides) -> dict:
    return {**_GOOD, **overrides}


_FIELD = "assistance_params.assistance_models.top_n"
_BAD = [
    pytest.param({"model": _GOOD["model"]}, "use 'assistance_models'", id="removed-model"),
    pytest.param({"model": None}, "use 'assistance_models'", id="removed-model-null"),
    pytest.param(
        {"assistance_models": {"top_n": _entry(model="gpt-4o")}}, _FIELD, id="unprefixed-model"
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(model="anthropic/claude-sonnet-4.6")}},
        "openrouter, openai",
        id="unknown-provider",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _GOOD["model"]}},
        "a bare model id is no longer accepted",
        id="bare-string",
    ),
    pytest.param({"assistance_models": {"top_n": 5}}, "must be a JSON object", id="non-object"),
    pytest.param(
        {"assistance_models": {"top_n": {"model": _GOOD["model"]}}},
        "missing required key(s) reasoning_effort, text_verbosity, temperature",
        id="missing-options",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(tools=["web_search"])}},
        "unknown key(s) tools",
        id="unknown-key",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(reasoning_effort="max")}},
        "'reasoning_effort' must be null or one of minimal, low, medium, high",
        id="bad-effort",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(text_verbosity="terse")}},
        "'text_verbosity' must be null or one of low, medium, high",
        id="bad-verbosity",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(temperature="0")}},
        "'temperature' must be null or a number from 0 to 2",
        id="string-temperature",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(temperature=3)}},
        "'temperature' must be null or a number from 0 to 2",
        id="temperature-out-of-range",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(temperature=True)}},
        "'temperature' must be null or a number from 0 to 2",
        id="bool-temperature",
    ),
    pytest.param(
        {"assistance_models": {"decomposition": _GOOD}}, "Unknown assistance method", id="unknown"
    ),
    pytest.param({"assistance_models": "x"}, "must be an object", id="non-object-map"),
]


@pytest.mark.parametrize(("params", "expected"), _BAD)
def test_create_rejects_a_bad_entry(client: TestClient, params, expected):
    resp = _create(client, params)
    assert resp.status_code == 400
    assert expected in resp.json()["detail"]


@pytest.mark.parametrize(("params", "expected"), _BAD)
def test_patch_rejects_a_bad_entry_and_stores_nothing(client: TestClient, params, expected):
    exp = _create(client, {"n": 3}).json()
    resp = _patch(client, exp["id"], params)
    assert resp.status_code == 400
    assert expected in resp.json()["detail"]
    assert _params(client, exp["id"]) == {"n": 3}


def test_the_removed_model_key_hints_at_reloading_a_stale_page(client: TestClient):
    """A page opened before the update re-sends `model` on every save."""
    created = _create(client, {"model": _GOOD["model"]})
    patched = _patch(client, _create(client).json()["id"], {"model": _GOOD["model"]})
    for resp in (created, patched):
        assert resp.status_code == 400
        assert "use 'assistance_models'" in resp.json()["detail"]
        assert "reload it" in resp.json()["detail"]


@pytest.mark.parametrize(
    "entry", [_GOOD, _OPENAI, _entry(temperature=0.5, reasoning_effort="high")]
)
def test_good_entries_and_clears_pass(client: TestClient, entry):
    exp = _create(client, {"assistance_models": {"top_n": entry}}).json()
    assert exp["assistance_params"] == {"assistance_models": {"top_n": entry}}
    assert exp["resolved_models"]["top_n"] == {**entry, "source": "assistance_models"}
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


def test_a_stored_bare_id_resolves_with_the_default_options(client: TestClient, sync_engine):
    """A row written before entries carried options runs as it did then."""
    exp = _create(client).json()
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET assistance_params = :p WHERE id = :id"),
            {
                "p": json.dumps({"assistance_models": {"top_n": "openrouter/test/legacy"}}),
                "id": exp["id"],
            },
        )
    detail = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert detail["resolved_models"]["top_n"] == {
        "model": "openrouter/test/legacy",
        "reasoning_effort": None,
        "text_verbosity": None,
        "temperature": 0,
        "source": "assistance_models",
    }
