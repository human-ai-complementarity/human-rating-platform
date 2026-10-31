"""Model entries and `num_samples` in `assistance_params` are validated on
create and PATCH, and the removed `model` key is refused.

Unvalidated, a bad entry fails only at rater time: `parse_model` raises or the
provider rejects an option, which the methods turn into a silent no-assistance
step. An unbounded `num_samples` is that many LLM calls per subtask per round.
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
_OAI = {
    "model": "oai/gpt-5.6-luna",
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
        "openrouter, oai",
        id="unknown-provider",
    ),
    pytest.param(
        # An OpenRouter id in the pipeline's wave files; "oai/" is the direct API.
        {"assistance_models": {"top_n": _entry(model="openai/gpt-5.6-luna")}},
        "openrouter, oai",
        id="openai-is-not-a-provider",
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
        {"assistance_models": {"top_n": _entry(max_tokens=100)}},
        "unknown key(s) max_tokens",
        id="unknown-key",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(tools="web_search")}},
        f"{_FIELD}: 'tools' must be a list of tool names from web_search, code_interpreter",
        id="tools-not-a-list",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(tools=None)}},
        "'tools' must be a list",
        id="tools-null",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(tools=["browser"])}},
        f"{_FIELD}: 'tools' has unknown tool(s) \"browser\"",
        id="unknown-tool",
    ),
    pytest.param(
        {"assistance_models": {"top_n": _entry(tools=["web_search", "web_search"])}},
        f"{_FIELD}: 'tools' lists web_search more than once",
        id="duplicate-tool",
    ),
    # Until the transport sends tools, an entry declaring any is refused
    # rather than run without them.
    pytest.param(
        {"assistance_models": {"top_n": _entry(tools=["web_search"])}},
        f"{_FIELD}: tools are not supported yet",
        id="tools-not-supported-yet",
    ),
    pytest.param(
        {"assistance_models": {"top_n": {**_OAI, "tools": ["code_interpreter"]}}},
        f"{_FIELD}: tools are not supported yet",
        id="oai-tools-not-supported-yet",
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
    # The instrument overrides stay bare ids, validated as such.
    pytest.param(
        {"confidence_model": "gpt-4o"}, "assistance_params.confidence_model", id="bad-override"
    ),
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
    "entry",
    [_GOOD, _OAI, _entry(temperature=0.5, reasoning_effort="high"), _entry(tools=[])],
)
def test_good_entries_and_clears_pass(client: TestClient, entry):
    exp = _create(client, {"assistance_models": {"top_n": entry}}).json()
    assert exp["assistance_params"] == {"assistance_models": {"top_n": entry}}
    assert exp["resolved_models"]["top_n"] == {
        "tools": [],
        **entry,
        "source": "assistance_models",
    }
    assert _patch(client, exp["id"], {"assistance_models": {"top_n": None}}).status_code == 200
    assert _patch(client, exp["id"], {"assistance_models": None}).status_code == 200


def test_adding_tools_to_a_stored_entry_is_validated(client: TestClient):
    """Only changed values are checked; the same entry with tools has changed."""
    exp = _create(client, {"assistance_models": {"top_n": _GOOD}}).json()
    resp = _patch(client, exp["id"], {"assistance_models": {"top_n": _entry(tools=["web_search"])}})
    assert resp.status_code == 400
    assert f"{_FIELD}: tools are not supported yet" in resp.json()["detail"]
    assert _params(client, exp["id"]) == {"assistance_models": {"top_n": _GOOD}}


def test_a_stored_entrys_tools_are_shown(client: TestClient, sync_engine):
    """What the Assistance tab lists, for an entry holding tools."""
    exp = _create(client).json()
    stored = {**_OAI, "tools": ["web_search", "code_interpreter"]}
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET assistance_params = :p WHERE id = :id"),
            {"p": json.dumps({"assistance_models": {"top_n": stored}}), "id": exp["id"]},
        )
    detail = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert detail["resolved_models"]["top_n"] == {**stored, "source": "assistance_models"}


def test_a_good_override_passes(client: TestClient):
    exp = _create(client, {"confidence_model": "oai/gpt-4o"}).json()
    assert exp["assistance_params"] == {"confidence_model": "oai/gpt-4o"}


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


def test_a_restated_entry_spelling_out_empty_tools_is_not_a_change(client: TestClient, sync_engine):
    """As on upload (`same_entry`): a stored entry re-sent with `"tools": []`
    added is the same entry, so a value that predates validation does not
    block the edit."""
    exp = _create(client).json()
    legacy = {"assistance_models": {"top_n": _entry(model="gpt-4o")}}
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET assistance_params = :p WHERE id = :id"),
            {"p": json.dumps(legacy), "id": exp["id"]},
        )

    restated = {"assistance_models": {"top_n": _entry(model="gpt-4o", tools=[])}}
    assert _patch(client, exp["id"], restated, name="Renamed").status_code == 200


def test_a_restated_entry_listing_its_tools_in_another_order_is_not_a_change(
    client: TestClient, sync_engine
):
    """The same tools in another order are the same entry (`same_entry`), so
    restating a stored entry that way is not validated as a new one."""
    exp = _create(client).json()
    stored = {"assistance_models": {"top_n": _entry(tools=["web_search", "code_interpreter"])}}
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET assistance_params = :p WHERE id = :id"),
            {"p": json.dumps(stored), "id": exp["id"]},
        )

    reordered = {"assistance_models": {"top_n": _entry(tools=["code_interpreter", "web_search"])}}
    assert _patch(client, exp["id"], reordered, name="Renamed").status_code == 200


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
        "tools": [],
        "source": "assistance_models",
    }


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
