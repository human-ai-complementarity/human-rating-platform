"""Questions with images (#114): upload, delivery to raters, and the media route."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from config import get_settings
from services import media
from test_characterization import (
    _build_parquet_bytes,
    _create_experiment,
    _rater_headers,
    _start_session,
)

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


def _next_question(client: TestClient, session: dict) -> dict:
    response = client.get("/api/raters/next-question", headers=_rater_headers(session))
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_stores_blocks_and_serves_them_with_fetchable_urls(
    client: TestClient, sync_engine, media_source: Path
):
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

    session = _start_session(client, experiment["id"])
    question = _next_question(client, session)
    assert question["question_text"] == "Screen after step 1:\n\nAllow the action?"
    assert question["parent_question_text"] == "Task: enable Do Not Track"
    assert question["parent_content"] is None
    content = question["content"]
    assert content[0] == blocks[0] and content[2] == blocks[2]
    image = content[1]
    assert image["type"] == "image" and image["mime_type"] == "image/png"
    assert "ref" not in image
    url = image["url"]
    assert url.startswith(f"/api/raters/media/{DIGEST}?")

    # The URL is the credential: no session header needed, bytes come back
    # with the sniffed type and a cache lifetime bounded by the link's.
    fetched = client.get(url)
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == PNG
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.headers["cache-control"].startswith("private, max-age=")

    # Re-pointing the link at another object, or altering its signature, fails.
    query = parse_qs(urlparse(url).query)
    other = client.get(
        f"/api/raters/media/{JPEG_DIGEST}", params={"exp": query["exp"][0], "sig": query["sig"][0]}
    )
    assert other.status_code == 403
    tampered = client.get(url[:-1] + ("0" if url[-1] != "0" else "1"))
    assert tampered.status_code == 403
    assert (
        client.get("/api/raters/media/not-a-digest", params={"exp": 1, "sig": "x"}).status_code
        == 404
    )


def test_expired_media_links_are_refused(client: TestClient, media_source: Path):
    secret = get_settings().effective_rater_session_secret
    stale = media.proxy_url(secret, DIGEST, ttl_seconds=60, now=time.time() - 120)
    response = client.get(stale)
    assert response.status_code == 403
    assert "expired" in response.json()["detail"]


def test_parent_rows_can_carry_images_too(client: TestClient, media_source: Path):
    experiment = _create_experiment(client)
    parent_blocks = [_text("Transcript so far:"), _image(JPEG_DIGEST, "image/jpeg")]
    response = _upload(
        client,
        experiment["id"],
        _csv(
            [
                {
                    "question_id": "traj",
                    "question_text": "Transcript so far:",
                    "content": json.dumps(parent_blocks),
                },
                {
                    "question_id": "verdict",
                    "question_text": "Was the agent honest?",
                    "options": "yes|no",
                    "parent_question_id": "traj",
                },
            ]
        ),
    )
    assert response.status_code == 200, response.text
    session = _start_session(client, experiment["id"])
    question = _next_question(client, session)
    assert question["question_text"] == "Was the agent honest?"
    assert question["content"] is None
    assert question["parent_content"][0] == parent_blocks[0]
    assert question["parent_content"][1]["mime_type"] == "image/jpeg"
    fetched = client.get(question["parent_content"][1]["url"])
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/jpeg"


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
    session = _start_session(client, experiment["id"])
    question = _next_question(client, session)
    assert question["content"] is None and question["parent_content"] is None


def test_serving_fails_closed_when_the_source_disappears(client: TestClient, media_source: Path):
    experiment = _create_experiment(client)
    response = _upload(
        client,
        experiment["id"],
        _csv([{"question_id": "q1", "question_text": "x", "content": json.dumps([_image()])}]),
    )
    assert response.status_code == 200, response.text
    session = _start_session(client, experiment["id"])
    settings = get_settings()
    settings.media.source_url = ""
    response = client.get("/api/raters/next-question", headers=_rater_headers(session))
    assert response.status_code == 503
    assert "MEDIA__SOURCE_URL" in response.json()["detail"]
