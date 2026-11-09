"""Reading media from its source and minting URLs raters can fetch it by."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from config import MediaSettings
from services import media
from services.media import source as media_source

PNG = b"\x89PNG\r\n\x1a\n" + b"not really a png"
DIGEST = hashlib.sha256(PNG).hexdigest()
SECRET = "rater-secret"


@pytest.fixture
def file_source(tmp_path: Path) -> MediaSettings:
    (tmp_path / DIGEST).write_bytes(PNG)
    return MediaSettings(source_url=f"file://{tmp_path}")


def test_unset_source_is_refused_with_a_pointer():
    settings = MediaSettings(source_url="")
    assert not media.configured(settings)
    with pytest.raises(media.MediaSourceError, match="MEDIA__SOURCE_URL is not set"):
        asyncio.run(media.read_bytes(settings, DIGEST))


def test_unknown_scheme_is_refused():
    # Caught at config load; the reader checks again for a value set by hand.
    with pytest.raises(ValidationError, match="must be gs://bucket/prefix or file://folder"):
        MediaSettings(source_url="https://example.com/media")
    bypassed = MediaSettings.model_construct(source_url="https://example.com/media")
    with pytest.raises(media.MediaSourceError, match="must start with gs:// or file://"):
        asyncio.run(media.read_bytes(bypassed, DIGEST))


def test_file_source_reads_and_verifies_the_digest(file_source: MediaSettings, tmp_path: Path):
    assert asyncio.run(media.read_bytes(file_source, DIGEST)) == PNG
    with pytest.raises(media.MediaNotFound, match="not found"):
        asyncio.run(media.read_bytes(file_source, "0" * 64))
    # An object that no longer hashes to its name is refused, not served.
    (tmp_path / DIGEST).write_bytes(PNG + b"!")
    with pytest.raises(media.MediaNotFound, match="no longer hashes"):
        asyncio.run(media.read_bytes(file_source, DIGEST))


def test_relative_file_source_resolves_against_the_backend_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(media_source, "BASE_DIR", tmp_path)
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / DIGEST).write_bytes(PNG)
    settings = MediaSettings(source_url="file://media")
    assert asyncio.run(media.read_bytes(settings, DIGEST)) == PNG


def test_missing_digests_names_what_the_source_lacks(file_source: MediaSettings):
    assert asyncio.run(media.missing_digests(file_source, [])) == []
    assert asyncio.run(media.missing_digests(file_source, [DIGEST, "0" * 64, "1" * 64])) == [
        "0" * 64,
        "1" * 64,
    ]


def test_proxy_url_is_a_bearer_link_that_expires():
    url = media.proxy_url(SECRET, DIGEST, ttl_seconds=600, now=1_000_000)
    parsed = urlparse(url)
    assert parsed.path == f"{media.MEDIA_PATH}/{DIGEST}"
    query = parse_qs(parsed.query)
    expires, signature = int(query["exp"][0]), query["sig"][0]
    assert expires == 1_000_600
    assert media.verify_proxy_token(SECRET, DIGEST, expires, signature, now=1_000_599)
    # Expired, tampered, re-pointed at another object, or signed elsewhere.
    assert not media.verify_proxy_token(SECRET, DIGEST, expires, signature, now=1_000_600)
    assert not media.verify_proxy_token(SECRET, DIGEST, expires, signature[:-1] + "0", now=0)
    assert not media.verify_proxy_token(SECRET, "0" * 64, expires, signature, now=0)
    assert not media.verify_proxy_token("other", DIGEST, expires, signature, now=0)


def test_render_blocks_turns_refs_into_proxy_urls(file_source: MediaSettings):
    blocks = [
        {"type": "text", "text": "Screen:"},
        {"type": "image", "ref": f"sha256:{DIGEST}", "mime_type": "image/png"},
    ]
    rendered = media.render_blocks(file_source, SECRET, blocks)
    assert rendered[0] == blocks[0]
    assert rendered[1]["type"] == "image"
    assert rendered[1]["mime_type"] == "image/png"
    assert rendered[1]["url"].startswith(f"{media.MEDIA_PATH}/{DIGEST}?exp=")
    # The content address stays server side.
    assert "ref" not in rendered[1]
    assert media.render_blocks(file_source, SECRET, None) is None


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (PNG, "image/png"),
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"GIF89a....", "application/octet-stream"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"<svg/>", "application/octet-stream"),
    ],
)
def test_sniff_mime_reads_the_bytes_not_the_label(data: bytes, mime: str):
    assert media.sniff_mime(data) == mime


def test_digests_are_listed_in_batches_not_one_call_each(monkeypatch: pytest.MonkeyPatch):
    """One listing request per batch of digests, each with its size, rather
    than an existence call per image."""
    held = {DIGEST: len(PNG), "1" * 64: 9_000_000}
    calls: list[dict] = []

    class _Blob:
        def __init__(self, name: str, size: int) -> None:
            self.name = name
            self.size = size

    class _Client:
        def list_blobs(self, bucket_name: str, *, prefix: str | None, match_glob: str):
            calls.append({"bucket": bucket_name, "prefix": prefix, "glob": match_glob})
            assert match_glob.startswith("media/{") and match_glob.endswith("}")
            wanted = match_glob[len("media/{") : -1].split(",")
            return [_Blob(f"media/{d}", held[d]) for d in wanted if d in held]

    monkeypatch.setattr(media_source, "_client", lambda settings: _Client())
    monkeypatch.setattr(media_source, "_LIST_BATCH", 2)
    settings = MediaSettings(source_url="gs://bucket/media")
    digests = [DIGEST, "0" * 64, "1" * 64]

    assert asyncio.run(media.object_sizes(settings, digests)) == held
    assert asyncio.run(media.missing_digests(settings, digests)) == ["0" * 64]
    # Three digests, batches of two: two requests per call, not three.
    assert len(calls) == 4
    assert calls[0] == {
        "bucket": "bucket",
        "prefix": "media/",
        "glob": "media/{" + DIGEST + "," + "0" * 64 + "}",
    }


def test_file_source_reports_sizes(file_source: MediaSettings):
    assert asyncio.run(media.object_sizes(file_source, [DIGEST, "0" * 64])) == {DIGEST: len(PNG)}
