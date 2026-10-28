"""Typed question content: text and image blocks.

A question's ``content`` is the inference pipeline's stored block form
(``pipeline/domain/content.py`` in that repo), so an export carries it over
verbatim::

    {"type": "text", "text": str}
    {"type": "image", "ref": "sha256:<64 hex>", "mime_type": "image/png"}

``ref`` is the SHA-256 of the image's bytes and names the object in the media
source (``services/media/source.py``). Blocks carry exactly their type's
keys. ``question_text`` stays the text-only form of the same stimulus, for
exports, analytics and anything else that reads only text.
"""

from __future__ import annotations

import json
import re
from typing import Any

Block = dict[str, Any]

TEXT = "text"
IMAGE = "image"
BLOCK_TYPES = frozenset({TEXT, IMAGE})

REF_PATTERN = re.compile(r"sha256:([0-9a-f]{64})")
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

_TEXT_KEYS = frozenset({"type", "text"})
_IMAGE_KEYS = frozenset({"type", "ref", "mime_type"})


class ContentError(ValueError):
    """A ``content`` cell is not a valid block list."""


def text_block(text: str) -> Block:
    return {"type": TEXT, "text": text}


def parse_content(raw: str | None, *, where: str) -> list[Block] | None:
    """The block list in a ``content`` cell; None for an empty cell.

    Raises ``ContentError`` naming ``where`` (the row) for anything that is
    not a non-empty list of well-formed blocks.
    """
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContentError(f"{where}: content must be a JSON array of blocks ({exc.msg})") from exc
    return validate_blocks(parsed, where=where)


def validate_blocks(parsed: Any, *, where: str) -> list[Block]:
    if not isinstance(parsed, list) or not parsed:
        raise ContentError(f"{where}: content must be a non-empty JSON array of blocks")
    for index, block in enumerate(parsed):
        label = f"{where}, block {index}"
        if not isinstance(block, dict):
            raise ContentError(f"{label}: a block must be an object, got {block!r}")
        kind = block.get("type")
        if kind == TEXT:
            if set(block) != _TEXT_KEYS or not isinstance(block["text"], str):
                raise ContentError(
                    f'{label}: a text block is exactly {{"type": "text", "text": str}}'
                )
        elif kind == IMAGE:
            if set(block) != _IMAGE_KEYS:
                raise ContentError(
                    f"{label}: an image block is exactly "
                    '{"type": "image", "ref": "sha256:<64 hex>", "mime_type": str}'
                )
            ref = block["ref"]
            if not isinstance(ref, str) or REF_PATTERN.fullmatch(ref) is None:
                raise ContentError(f"{label}: ref must be 'sha256:<64 hex digits>', got {ref!r}")
            mime_type = block["mime_type"]
            if mime_type not in IMAGE_MIME_TYPES:
                raise ContentError(
                    f"{label}: mime_type must be one of {sorted(IMAGE_MIME_TYPES)}, "
                    f"got {mime_type!r}"
                )
        else:
            raise ContentError(f"{label}: unknown block type {kind!r}")
    return parsed


def load_content(stored: str | None) -> list[Block] | None:
    """The blocks a ``Question.content`` column holds; validated on upload."""
    if not stored:
        return None
    return json.loads(stored)


def dump_content(blocks: list[Block] | None) -> str | None:
    return json.dumps(blocks, separators=(",", ":")) if blocks else None


def content_text(blocks: list[Block]) -> str:
    """The text of a block list: text blocks joined, as the pipeline joins them."""
    return "\n\n".join(b["text"] for b in blocks if b["type"] == TEXT)


def image_digests(blocks: list[Block] | None) -> list[str]:
    """Every image's hex digest, in order, without repeats."""
    seen: dict[str, None] = {}
    for block in blocks or ():
        if block["type"] == IMAGE:
            seen.setdefault(REF_PATTERN.fullmatch(block["ref"]).group(1))
    return list(seen)


def has_media(blocks: list[Block] | None) -> bool:
    return any(b["type"] != TEXT for b in blocks or ())
