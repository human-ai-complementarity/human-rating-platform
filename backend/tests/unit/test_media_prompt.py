"""Content blocks as chat-completion parts for assistance models."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path

import pytest

from config import MediaSettings
from services.media import prompt
from services.media.blocks import text_block

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
DIGEST = hashlib.sha256(PNG).hexdigest()


@pytest.fixture
def file_source(tmp_path: Path) -> MediaSettings:
    (tmp_path / DIGEST).write_bytes(PNG)
    prompt._encoded.clear()
    return MediaSettings(source_url=f"file://{tmp_path}")


def test_text_only_stimulus_is_one_text_part(file_source: MediaSettings):
    assert asyncio.run(prompt.parts_for(file_source, "Is it blue?", None)) == [
        text_block("Is it blue?")
    ]


def test_blocks_become_parts_in_order_with_images_inline(file_source: MediaSettings):
    blocks = [
        text_block("Screen after step 1:"),
        {"type": "image", "ref": f"sha256:{DIGEST}", "mime_type": "image/png"},
        text_block("Allowed?"),
    ]
    parts = asyncio.run(prompt.parts_for(file_source, "ignored when blocks exist", blocks))
    assert parts[0] == text_block("Screen after step 1:")
    assert parts[2] == text_block("Allowed?")
    url = parts[1]["image_url"]["url"]
    assert parts[1]["type"] == "image_url"
    assert url == "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")


def test_encoded_images_are_kept_for_the_next_call(file_source: MediaSettings, tmp_path: Path):
    block = {"type": "image", "ref": f"sha256:{DIGEST}", "mime_type": "image/png"}
    first = asyncio.run(prompt.parts_for(file_source, "", [block]))
    (tmp_path / DIGEST).unlink()
    second = asyncio.run(prompt.parts_for(file_source, "", [block]))
    assert first == second


def test_collapse_joins_text_and_returns_a_string_when_all_text():
    assert prompt.collapse(
        [text_block("Question:\n"), text_block("x"), text_block("\n\nOptions")]
    ) == ("Question:\nx\n\nOptions")
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    mixed = prompt.collapse([text_block("A"), text_block("B"), image, text_block("C")])
    assert mixed == [text_block("AB"), image, text_block("C")]
    assert prompt.collapse([]) == ""
