"""`render_markdown` as a dataset_meta key: the export switches `is_markdown` on.

The same treatment as `render_math` (test_upload_render_math.py): a JSON
boolean, switched on by an upload but never off, and free to combine with
the other rendering flag.
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
        json={"name": "Render markdown meta", "assistance_method": "none"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload_csv(client: TestClient, experiment_id: int, meta: dict):
    csv_data = (
        f"#META: {json.dumps(meta)}\n"
        "question_id,question_text,gt_answer,options,question_type\n"
        "q1,Is `x` **bold**?,Yes,Yes|No,MC\n"
    )
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("prose_n1.csv", csv_data, "text/csv")},
    )


def _upload_parquet(client: TestClient, experiment_id: int, meta: dict):
    table = pa.Table.from_pylist(
        [{"question_id": "q1", "question_text": "Is `x` **bold**?", "question_type": "MC"}]
    ).replace_schema_metadata({b"dataset_meta": json.dumps(meta).encode("utf-8")})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("prose_n1.parquet", buf.getvalue(), "application/octet-stream")},
    )


def _flags(client: TestClient, experiment_id: int) -> tuple[bool, bool]:
    experiment = client.get(f"/api/admin/experiments/{experiment_id}").json()
    return experiment["is_markdown"], experiment["render_math"]


def _patch(client: TestClient, experiment_id: int, **fields):
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}",
        json={"assistance_method": "none", **fields},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("upload", [_upload_csv, _upload_parquet])
def test_declaring_true_switches_markdown_on(client: TestClient, upload):
    exp = _experiment(client)
    resp = upload(client, exp["id"], {"render_markdown": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["meta_applied"] == ["render_markdown"]
    assert resp.json()["meta_conflicts"] == []
    assert _flags(client, exp["id"]) == (True, False)
    uploads = client.get(f"/api/admin/experiments/{exp['id']}/uploads").json()
    assert uploads[0]["dataset_meta"] == {"render_markdown": True}


@pytest.mark.parametrize("value", ["true", 0])
def test_a_non_boolean_rejects_the_upload(client: TestClient, value):
    exp = _experiment(client)
    resp = _upload_csv(client, exp["id"], {"render_markdown": value})

    assert resp.status_code == 400
    assert "render_markdown must be true or false" in resp.json()["detail"]
    assert _flags(client, exp["id"]) == (False, False)


@pytest.mark.parametrize("on", [True, False])
def test_an_upload_without_the_key_leaves_markdown_as_it_was(client: TestClient, on):
    exp = _experiment(client)
    _patch(client, exp["id"], is_markdown=on)
    body = _upload_csv(client, exp["id"], {"description": "A guide."}).json()

    assert body["meta_applied"] == ["description"]
    assert body["meta_conflicts"] == []
    assert _flags(client, exp["id"]) == (on, False)


def test_declaring_false_leaves_an_admins_choice_and_is_reported(client: TestClient):
    exp = _experiment(client)
    _patch(client, exp["id"], is_markdown=True)
    body = _upload_csv(client, exp["id"], {"render_markdown": False}).json()

    assert body["meta_applied"] == []
    assert body["meta_conflicts"] == ["render_markdown"]
    assert _flags(client, exp["id"]) == (True, False)


def test_declaring_true_switches_markdown_on_for_a_math_experiment(client: TestClient):
    exp = _experiment(client)
    _patch(client, exp["id"], render_math=True)
    body = _upload_csv(client, exp["id"], {"render_markdown": True}).json()

    assert body["meta_applied"] == ["render_markdown"]
    assert body["meta_conflicts"] == []
    assert _flags(client, exp["id"]) == (True, True)


def test_declaring_both_rendering_flags_switches_both_on(client: TestClient):
    exp = _experiment(client)
    resp = _upload_csv(client, exp["id"], {"render_markdown": True, "render_math": True})

    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["meta_applied"]) == ["render_markdown", "render_math"]
    assert resp.json()["meta_conflicts"] == []
    assert _flags(client, exp["id"]) == (True, True)
