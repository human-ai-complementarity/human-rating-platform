"""Questions with images (#114): the content column and the media source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from config import get_settings
from services import media
from test_characterization import _build_parquet_bytes, _create_experiment

PNG = b"\x89PNG\r\n\x1a\n" + b"screenshot bytes"
DIGEST = hashlib.sha256(PNG).hexdigest()
JPEG = b"\xff\xd8\xff\xe0" + b"photo bytes"
JPEG_DIGEST = hashlib.sha256(JPEG).hexdigest()


def _image(digest: str = DIGEST, mime: str = "image/png") -> dict:
    return {"type": "image", "ref": f"sha256:{digest}", "mime_type": mime}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


@pytest.fixture
def media_source(tmp_path: Path):
    root = tmp_path / "media"
    root.mkdir()
    (root / DIGEST).write_bytes(PNG)
    (root / JPEG_DIGEST).write_bytes(JPEG)
    settings = get_settings()
    original = settings.media.source_url
    settings.media.source_url = f"file://{root}"
    yield root
    settings.media.source_url = original


def _csv(rows: list[dict]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=["question_id", "question_text", "options", "parent_question_id", "content"],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({"options": "", "parent_question_id": "", "content": "", **row})
    return buffer.getvalue()


def _upload(client: TestClient, experiment_id: int, body, *, name: str = "q.csv"):
    content_type = "text/csv" if name.endswith(".csv") else "application/octet-stream"
    return client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": (name, body, content_type)},
    )


def _stored_content(sync_engine, experiment_id: int) -> dict[str, str | None]:
    with sync_engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT question_id, content FROM questions WHERE experiment_id = :id"),
                {"id": experiment_id},
            ).all()
        )


def test_upload_stores_blocks_as_given(client: TestClient, sync_engine, media_source: Path):
    experiment = _create_experiment(client)
    blocks = [_text("Screen after step 1:"), _image(), _text("Allow the action?")]
    response = _upload(
        client,
        experiment["id"],
        _csv(
            [
                {"question_id": "task", "question_text": "Task: enable Do Not Track"},
                {
                    "question_id": "step1",
                    "question_text": media.content_text(blocks),
                    "options": "allow|deny",
                    "parent_question_id": "task",
                    "content": json.dumps(blocks),
                },
            ]
        ),
    )
    assert response.status_code == 200, response.text
    stored = _stored_content(sync_engine, experiment["id"])
    assert stored["task"] is None
    assert json.loads(stored["step1"]) == blocks


def test_parquet_content_column_may_be_typed_blocks(
    client: TestClient, sync_engine, media_source: Path
):
    # A list<struct> column gives every block the union of the fields; the
    # absent ones arrive as nulls and must not survive into the stored JSON.
    blocks = [_text("Screen:"), _image()]
    rows = [
        {"question_id": "q1", "question_text": "Screen:", "question_type": "MC", "content": blocks},
        {"question_id": "q2", "question_text": "Plain", "question_type": "MC", "content": None},
    ]
    experiment = _create_experiment(client)
    body = _build_parquet_bytes(rows=rows)
    response = _upload(client, experiment["id"], body, name="q.parquet")
    assert response.status_code == 200, response.text
    stored = _stored_content(sync_engine, experiment["id"])
    assert json.loads(stored["q1"]) == blocks
    assert stored["q2"] is None


def test_upload_refuses_images_the_source_does_not_hold(client: TestClient, media_source: Path):
    experiment = _create_experiment(client)
    missing = "0" * 64
    response = _upload(
        client,
        experiment["id"],
        _csv(
            [
                {
                    "question_id": "q1",
                    "question_text": "x",
                    "content": json.dumps([_image(), _image(missing)]),
                }
            ]
        ),
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "1 referenced image(s) are not in the media source" in detail
    assert f"sha256:{missing}" in detail
    assert DIGEST not in detail
    assert (
        client.get(f"/api/admin/experiments/{experiment['id']}/stats").json()["total_questions"]
        == 0
    )


def test_upload_refuses_images_without_a_media_source(client: TestClient):
    assert get_settings().media.source_url == ""
    experiment = _create_experiment(client)
    response = _upload(
        client,
        experiment["id"],
        _csv([{"question_id": "q1", "question_text": "x", "content": json.dumps([_image()])}]),
    )
    assert response.status_code == 400
    assert "MEDIA__SOURCE_URL is not set" in response.json()["detail"]


def test_upload_refuses_malformed_content_naming_the_row(client: TestClient):
    experiment = _create_experiment(client)
    response = _upload(
        client,
        experiment["id"],
        _csv([{"question_id": "q9", "question_text": "x", "content": '[{"type": "video"}]'}]),
    )
    assert response.status_code == 400
    assert response.json()["detail"].startswith(
        "Question 'q9', block 0: unknown block type 'video'"
    )


def test_text_only_uploads_are_untouched(client: TestClient, sync_engine):
    experiment = _create_experiment(client)
    response = _upload(
        client, experiment["id"], _csv([{"question_id": "q1", "question_text": "x"}])
    )
    assert response.status_code == 200, response.text
    assert _stored_content(sync_engine, experiment["id"]) == {"q1": None}
