"""`assistance_models` as a dataset_meta key: the wave's model per method.

One export file usually serves both arms of a wave, so it declares a model per
assisted method. The old single `model` key is refused.
"""

from __future__ import annotations

import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

_TOP_N = "openrouter/anthropic/claude-sonnet-4.6"
_HAAT = "openrouter/google/gemini-3-flash-preview"
_OTHER = "openrouter/openai/gpt-4o"
_MODELS = {"top_n": _TOP_N, "human_as_a_tool": _HAAT}
_BOTH = ["assistance_models.human_as_a_tool", "assistance_models.top_n"]


def _experiment(client: TestClient, **payload) -> dict:
    payload.setdefault("name", "Assistance models meta")
    payload.setdefault("assistance_method", "top_n")
    resp = client.post("/api/admin/experiments", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload_csv(client: TestClient, experiment_id: int, meta: dict):
    csv_data = (
        f"#META: {json.dumps(meta)}\n"
        "question_id,question_text,gt_answer,options,question_type\n"
        "q1,Is this useful?,Yes,Yes|No,MC\n"
    )
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("medqa_n1.csv", csv_data, "text/csv")},
    )


def _upload_parquet(client: TestClient, experiment_id: int, meta: dict):
    table = pa.Table.from_pylist(
        [{"question_id": "q1", "question_text": "Is this useful?", "question_type": "MC"}]
    ).replace_schema_metadata({b"dataset_meta": json.dumps(meta).encode("utf-8")})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("medqa_n1.parquet", buf.getvalue(), "application/octet-stream")},
    )


def _params(client: TestClient, experiment_id: int) -> dict | None:
    return client.get(f"/api/admin/experiments/{experiment_id}").json()["assistance_params"]


def _patch_params(client: TestClient, experiment_id: int, params: dict) -> None:
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}",
        json={"assistance_method": "top_n", "assistance_params": params},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_the_map_is_pinned_on_upload(client: TestClient, upload):
    exp = _experiment(client, assistance_params={"n": 4})
    resp = upload(client, exp["id"], {"assistance_models": _MODELS})

    assert resp.status_code == 200, resp.text
    assert resp.json()["meta_applied"] == _BOTH
    assert resp.json()["meta_conflicts"] == []
    assert _params(client, exp["id"]) == {"n": 4, "assistance_models": _MODELS}
    uploads = client.get(f"/api/admin/experiments/{exp['id']}/uploads").json()
    assert uploads[0]["dataset_meta"] == {"assistance_models": _MODELS}


@pytest.mark.parametrize("model", [_OTHER, None])
@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_the_removed_model_key_rejects_the_upload(client: TestClient, upload, model):
    exp = _experiment(client, assistance_params={"n": 4})
    resp = upload(client, exp["id"], {"model": model, "assistance_models": _MODELS})

    assert resp.status_code == 400
    assert "use 'assistance_models'" in resp.json()["detail"]
    assert _params(client, exp["id"]) == {"n": 4}


def test_an_upload_without_the_map_leaves_assistance_params_alone(client: TestClient):
    exp = _experiment(client, assistance_params={"n": 4})
    body = _upload_csv(client, exp["id"], {"description": "A guide."}).json()

    assert body["meta_applied"] == ["description"]
    assert _params(client, exp["id"]) == {"n": 4}


def test_a_method_already_set_is_reported_not_clobbered(client: TestClient):
    exp = _experiment(client, assistance_params={"assistance_models": {"top_n": _OTHER}})
    body = _upload_csv(client, exp["id"], {"assistance_models": _MODELS}).json()

    assert body["meta_applied"] == ["assistance_models.human_as_a_tool"]
    assert body["meta_conflicts"] == ["assistance_models.top_n"]
    assert _params(client, exp["id"])["assistance_models"] == {
        "top_n": _OTHER,
        "human_as_a_tool": _HAAT,
    }


def test_a_later_upload_agreeing_is_silent_and_disagreeing_is_reported(client: TestClient):
    exp = _experiment(client)
    _upload_csv(client, exp["id"], {"assistance_models": _MODELS})

    same = _upload_csv(client, exp["id"], {"assistance_models": _MODELS}).json()
    assert same["meta_applied"] == [] and same["meta_conflicts"] == []

    differs = _upload_csv(client, exp["id"], {"assistance_models": {"top_n": _OTHER}}).json()
    assert differs["meta_conflicts"] == ["assistance_models.top_n"]
    assert _params(client, exp["id"])["assistance_models"] == _MODELS


@pytest.mark.parametrize(
    ("cleared", "conflicts"),
    [({"top_n": None}, ["assistance_models.top_n"]), (None, _BOTH)],
)
def test_a_deliberately_cleared_entry_is_not_repinned(client: TestClient, cleared, conflicts):
    exp = _experiment(client)
    _patch_params(client, exp["id"], {"assistance_models": cleared})

    body = _upload_csv(client, exp["id"], {"assistance_models": _MODELS}).json()
    assert sorted(body["meta_conflicts"]) == conflicts
    if cleared is None:
        assert _params(client, exp["id"])["assistance_models"] is None
    else:
        assert _params(client, exp["id"])["assistance_models"]["top_n"] is None


def test_a_patch_of_other_params_keeps_the_map(client: TestClient):
    exp = _experiment(client, assistance_params={"n": 3})
    _upload_csv(client, exp["id"], {"assistance_models": _MODELS})

    _patch_params(client, exp["id"], {"n": 5})
    assert _params(client, exp["id"]) == {"n": 5, "assistance_models": _MODELS}


def test_a_patch_of_one_method_keeps_the_others_and_their_null_markers(client: TestClient):
    exp = _experiment(client)
    _upload_csv(client, exp["id"], {"assistance_models": _MODELS})

    _patch_params(client, exp["id"], {"assistance_models": {"top_n": None}})
    _patch_params(client, exp["id"], {"assistance_models": {"human_as_a_tool": _OTHER}})
    assert _params(client, exp["id"])["assistance_models"] == {
        "top_n": None,
        "human_as_a_tool": _OTHER,
    }

    body = _upload_csv(client, exp["id"], {"assistance_models": _MODELS}).json()
    assert body["meta_conflicts"] == _BOTH
    assert _params(client, exp["id"])["assistance_models"]["top_n"] is None


def test_the_map_inherits_the_config_lock(client: TestClient, sync_engine):
    exp = _experiment(client, assistance_params={"n": 3})
    _upload_csv(client, exp["id"], {"assistance_models": _MODELS})
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET status = 'LAUNCH' WHERE id = :id"), {"id": exp["id"]}
        )
    url = f"/api/admin/experiments/{exp['id']}"

    resent = client.patch(
        url, json={"assistance_method": "top_n", "assistance_params": _params(client, exp["id"])}
    )
    assert resent.status_code == 200, resent.text

    restated = client.patch(
        url,
        json={
            "assistance_method": "top_n",
            "assistance_params": {"assistance_models": {"top_n": _TOP_N}},
        },
    )
    assert restated.status_code == 200, restated.text

    changed = client.patch(
        url,
        json={
            "assistance_method": "top_n",
            "assistance_params": {"assistance_models": {**_MODELS, "top_n": _OTHER}},
        },
    )
    assert changed.status_code == 400
    assert "assistance_params" in changed.json()["detail"]


@pytest.mark.parametrize(
    ("assistance_models", "expected"),
    [
        ({"none": _TOP_N}, "none"),
        ({"isd": _HAAT}, "isd"),
        ({"top_n": "claude-sonnet-4-6"}, "assistance_models.top_n"),
        ({"human_as_a_tool": 3}, "assistance_models.human_as_a_tool"),
        ({"top_n": ""}, "assistance_models.top_n"),
        ({"top_n": None}, "assistance_models.top_n"),
        (_TOP_N, "must be a JSON object"),
        ([_TOP_N], "must be a JSON object"),
    ],
)
@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_a_bad_map_rejects_the_upload(client: TestClient, upload, assistance_models, expected):
    exp = _experiment(client)
    resp = upload(client, exp["id"], {"assistance_models": assistance_models})

    assert resp.status_code == 400
    assert expected in resp.json()["detail"]
    assert _params(client, exp["id"]) is None
