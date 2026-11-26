"""Content blocks: the pipeline's stored form, validated at upload."""

from __future__ import annotations

import json

import pytest

from services import media

DIGEST = "a" * 64
IMAGE = {"type": "image", "ref": f"sha256:{DIGEST}", "mime_type": "image/png"}
TEXT = {"type": "text", "text": "Screen after step 1:"}


def test_empty_cell_is_no_content():
    assert media.parse_content(None, where="q1") is None
    assert media.parse_content("", where="q1") is None
    assert media.parse_content("   ", where="q1") is None


def test_well_formed_blocks_round_trip():
    blocks = media.parse_content(json.dumps([TEXT, IMAGE]), where="q1")
    assert blocks == [TEXT, IMAGE]
    stored = media.dump_content(blocks)
    assert media.load_content(stored) == blocks
    assert media.dump_content(None) is None
    assert media.load_content(None) is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "must be a JSON array"),
        ("{}", "non-empty JSON array"),
        ("[]", "non-empty JSON array"),
        ('["text"]', "must be an object"),
        ('[{"type": "audio"}]', "unknown block type 'audio'"),
        ('[{"type": "text"}]', "a text block is exactly"),
        ('[{"type": "text", "text": 1}]', "a text block is exactly"),
        ('[{"type": "text", "text": "x", "extra": 1}]', "a text block is exactly"),
        ('[{"type": "image", "ref": "sha256:%s"}]' % DIGEST, "an image block is exactly"),
        (
            '[{"type": "image", "ref": "images/a.png", "mime_type": "image/png"}]',
            "ref must be 'sha256:<64 hex digits>'",
        ),
        (
            '[{"type": "image", "ref": "sha256:%s", "mime_type": "image/bmp"}]' % DIGEST,
            "mime_type must be one of",
        ),
    ],
)
def test_malformed_blocks_are_refused_naming_the_row(raw: str, message: str):
    with pytest.raises(media.ContentError, match=message) as exc_info:
        media.parse_content(raw, where="Question 'q7'")
    assert "Question 'q7'" in str(exc_info.value)


def test_text_and_digests_read_the_blocks():
    other = {"type": "image", "ref": f"sha256:{'b' * 64}", "mime_type": "image/jpeg"}
    blocks = [TEXT, IMAGE, {"type": "text", "text": "then"}, other, IMAGE]
    assert media.content_text(blocks) == "Screen after step 1:\n\nthen"
    assert media.image_digests(blocks) == [DIGEST, "b" * 64]
    assert media.image_digests(None) == []
    assert media.has_media(blocks)
    assert not media.has_media([TEXT])
    assert not media.has_media(None)
