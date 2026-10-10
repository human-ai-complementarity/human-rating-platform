"""The expand-`assistance_models`-to-entries migration, run against the database."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/20261009000000_expand_assistance_models_to_entries.py"
)
_M = "openrouter/test/model"
_TOP_N_ENTRY = {"model": _M, "reasoning_effort": None, "text_verbosity": None, "temperature": 0}
_HAAT_ENTRY = {"model": _M, "reasoning_effort": None, "text_verbosity": None, "temperature": None}
_DECLARED = {
    "model": "openai/x",
    "reasoning_effort": "low",
    "text_verbosity": "low",
    "temperature": None,
}


def _migration():
    spec = importlib.util.spec_from_file_location("expand_entries_rev_db", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _experiment(client: TestClient, sync_engine, raw_params: str | None) -> int:
    resp = client.post(
        "/api/admin/experiments", json={"name": "Expand", "assistance_method": "top_n"}
    )
    assert resp.status_code == 200, resp.text
    experiment_id = resp.json()["id"]
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET assistance_params = :p WHERE id = :id"),
            {"p": raw_params, "id": experiment_id},
        )
    return experiment_id


def _stored(sync_engine) -> dict[int, str | None]:
    with sync_engine.connect() as conn:
        return dict(conn.execute(text("SELECT id, assistance_params FROM experiments")).all())


def test_it_expands_strings_with_each_methods_options_and_leaves_the_rest(
    client: TestClient, sync_engine
):
    both = _experiment(
        client,
        sync_engine,
        json.dumps({"n": 4, "assistance_models": {"top_n": _M, "human_as_a_tool": _M}}),
    )
    mixed = _experiment(
        client,
        sync_engine,
        json.dumps({"assistance_models": {"top_n": None, "human_as_a_tool": _M, "later": _M}}),
    )
    already = _experiment(
        client, sync_engine, json.dumps({"assistance_models": {"top_n": _DECLARED}})
    )
    cleared = _experiment(client, sync_engine, json.dumps({"assistance_models": None}))
    no_map = _experiment(client, sync_engine, json.dumps({"n": 2}))
    no_params = _experiment(client, sync_engine, None)
    not_json = _experiment(client, sync_engine, '"assistance_models" but not JSON')

    with sync_engine.begin() as conn:
        changed = _migration().expand_all(conn)
        again = _migration().expand_all(conn)

    assert (changed, again) == (2, 0)
    stored = _stored(sync_engine)
    assert json.loads(stored[both]) == {
        "n": 4,
        "assistance_models": {"top_n": _TOP_N_ENTRY, "human_as_a_tool": _HAAT_ENTRY},
    }
    assert json.loads(stored[mixed]) == {
        "assistance_models": {"top_n": None, "human_as_a_tool": _HAAT_ENTRY, "later": _HAAT_ENTRY}
    }
    assert json.loads(stored[already]) == {"assistance_models": {"top_n": _DECLARED}}
    assert json.loads(stored[cleared]) == {"assistance_models": None}
    assert json.loads(stored[no_map]) == {"n": 2}
    assert stored[no_params] is None
    assert stored[not_json] == '"assistance_models" but not JSON'

    # The expanded rows resolve to exactly what the methods sent before.
    detail = client.get(f"/api/admin/experiments/{both}").json()
    assert detail["resolved_models"] == {
        "top_n": {**_TOP_N_ENTRY, "source": "assistance_models"},
        "human_as_a_tool": {**_HAAT_ENTRY, "source": "assistance_models"},
    }


def test_downgrade_collapses_entries_back_to_ids(client: TestClient, sync_engine):
    expanded = _experiment(
        client,
        sync_engine,
        json.dumps({"assistance_models": {"top_n": _TOP_N_ENTRY, "human_as_a_tool": None}}),
    )
    declared = _experiment(
        client, sync_engine, json.dumps({"assistance_models": {"top_n": _DECLARED}})
    )

    with sync_engine.begin() as conn:
        changed = _migration().collapse_all(conn)

    assert changed == 2
    stored = _stored(sync_engine)
    assert json.loads(stored[expanded]) == {
        "assistance_models": {"top_n": _M, "human_as_a_tool": None}
    }
    assert json.loads(stored[declared]) == {"assistance_models": {"top_n": "openai/x"}}
