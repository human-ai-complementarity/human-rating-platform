"""`render_math` as a dataset_meta key: the export switches math rendering on.

A JSON boolean, not a string like the column-backed keys. An upload may turn
it on; it never turns off an experiment an admin switched on.
"""

from __future__ import annotations

import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient


def _experiment(client: TestClient) -> dict:
    resp = client.post(
        "/api/admin/experiments",
        json={"name": "Render math meta", "assistance_method": "none"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload_csv(client: TestClient, experiment_id: int, meta: dict):
    csv_data = (
        f"#META: {json.dumps(meta)}\n"
        "question_id,question_text,gt_answer,options,question_type\n"
        "q1,What is $E = mc^2$?,Yes,Yes|No,MC\n"
    )
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("physics_n1.csv", csv_data, "text/csv")},
    )


def _upload_parquet(client: TestClient, experiment_id: int, meta: dict):
    table = pa.Table.from_pylist(
        [{"question_id": "q1", "question_text": "What is $E = mc^2$?", "question_type": "MC"}]
    ).replace_schema_metadata({b"dataset_meta": json.dumps(meta).encode("utf-8")})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("physics_n1.parquet", buf.getvalue(), "application/octet-stream")},
    )


def _render_math(client: TestClient, experiment_id: int) -> bool:
    return client.get(f"/api/admin/experiments/{experiment_id}").json()["render_math"]


def _switch_on(client: TestClient, experiment_id: int) -> None:
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}",
        json={"assistance_method": "none", "render_math": True},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_declaring_true_switches_it_on(client: TestClient, upload):
    exp = _experiment(client)
    resp = upload(client, exp["id"], {"render_math": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["meta_applied"] == ["render_math"]
    assert resp.json()["meta_conflicts"] == []
    assert _render_math(client, exp["id"]) is True
    uploads = client.get(f"/api/admin/experiments/{exp['id']}/uploads").json()
    assert uploads[0]["dataset_meta"] == {"render_math": True}


@pytest.mark.parametrize("value", ["true", "false", 1, 0])
@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_a_non_boolean_rejects_the_upload(client: TestClient, upload, value):
    exp = _experiment(client)
    resp = upload(client, exp["id"], {"render_math": value})

    assert resp.status_code == 400
    assert "render_math must be true or false" in resp.json()["detail"]
    assert _render_math(client, exp["id"]) is False


def test_declaring_false_leaves_an_admins_choice_and_is_reported(client: TestClient):
    exp = _experiment(client)
    _switch_on(client, exp["id"])
    body = _upload_csv(client, exp["id"], {"render_math": False}).json()

    assert body["meta_applied"] == []
    assert body["meta_conflicts"] == ["render_math"]
    assert _render_math(client, exp["id"]) is True


@pytest.mark.parametrize("on", [True, False])
def test_declaring_the_current_value_is_silent(client: TestClient, on):
    exp = _experiment(client)
    if on:
        _switch_on(client, exp["id"])
    body = _upload_csv(client, exp["id"], {"render_math": on}).json()

    assert body["meta_applied"] == [] and body["meta_conflicts"] == []
    assert _render_math(client, exp["id"]) is on


def test_an_upload_without_the_key_leaves_it_off(client: TestClient):
    exp = _experiment(client)
    body = _upload_csv(client, exp["id"], {"description": "A guide."}).json()

    assert body["meta_applied"] == ["description"]
    assert _render_math(client, exp["id"]) is False
