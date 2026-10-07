"""The fold-`model`-into-`assistance_models` migration, run against the database."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/20261006000000_fold_assistance_model_into_map.py"
)
_M = "openrouter/test/model"


def _migration():
    spec = importlib.util.spec_from_file_location("fold_model_rev_db", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _experiment(client: TestClient, sync_engine, method: str, raw_params: str | None) -> int:
    resp = client.post("/api/admin/experiments", json={"name": "Fold", "assistance_method": method})
    assert resp.status_code == 200, resp.text
    experiment_id = resp.json()["id"]
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET assistance_params = :p WHERE id = :id"),
            {"p": raw_params, "id": experiment_id},
        )
    return experiment_id


def test_it_rewrites_only_rows_with_a_model(client: TestClient, sync_engine):
    top_n = _experiment(client, sync_engine, "top_n", json.dumps({"n": 4, "model": _M}))
    unassisted = _experiment(client, sync_engine, "none", json.dumps({"model": _M}))
    cleared = _experiment(client, sync_engine, "top_n", json.dumps({"model": None}))
    untouched = _experiment(
        client, sync_engine, "top_n", json.dumps({"assistance_models": {"top_n": _M}})
    )
    no_params = _experiment(client, sync_engine, "none", None)
    not_json = _experiment(client, sync_engine, "top_n", '"model" but not JSON')

    with sync_engine.begin() as conn:
        changed = _migration().fold_model_into_maps(conn)
        again = _migration().fold_model_into_maps(conn)

    assert (changed, again) == (3, 0)
    with sync_engine.connect() as conn:
        stored = dict(conn.execute(text("SELECT id, assistance_params FROM experiments")).all())
    assert json.loads(stored[top_n]) == {"n": 4, "assistance_models": {"top_n": _M}}
    assert json.loads(stored[unassisted]) == {
        "assistance_models": {"top_n": _M, "human_as_a_tool": _M}
    }
    assert stored[cleared] is None
    assert json.loads(stored[untouched]) == {"assistance_models": {"top_n": _M}}
    assert stored[no_params] is None
    assert stored[not_json] == '"model" but not JSON'
