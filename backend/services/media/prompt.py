"""Question content as chat-completion message parts, for assistance models.

Both providers the platform calls (OpenRouter and OpenAI's own API, through
chat completions) take a user message's content as a list of parts::

    {"type": "text", "text": "..."}
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}

Images go inline as base64, read from the media source and verified against
their digest, as the inference pipeline sends them: a provider never needs
to reach the bucket. A message that ends up all text collapses back to one
string, so a text-only question is sent exactly as it was before content
blocks existed.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from typing import Any

from config import MediaSettings

from .blocks import IMAGE, TEXT, Block, text_block
from .source import read_bytes

Part = dict[str, Any]

# Encoded images kept for the calls that follow: a question's images are
# asked for again by its own retries and by every rater who reaches it. The
# budget is bytes, not entries, since one screenshot is a few MB and the API
# process is small.
_ENCODED_BUDGET_BYTES = 32 * 1024 * 1024
_encoded: OrderedDict[str, str] = OrderedDict()
_encoded_bytes = 0


async def _data_url(settings: MediaSettings, digest: str, mime_type: str) -> str:
    global _encoded_bytes
    cached = _encoded.get(digest)
    if cached is not None:
        _encoded.move_to_end(digest)
        return cached
    data = await read_bytes(settings, digest)
    cached = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
    if len(cached) <= _ENCODED_BUDGET_BYTES:
        _encoded[digest] = cached
        _encoded_bytes += len(cached)
        while _encoded_bytes > _ENCODED_BUDGET_BYTES:
            _, evicted = _encoded.popitem(last=False)
            _encoded_bytes -= len(evicted)
    return cached


def clear_cache() -> None:
    global _encoded_bytes
    _encoded.clear()
    _encoded_bytes = 0


def image_part(data_url: str) -> Part:
    return {"type": "image_url", "image_url": {"url": data_url}}


async def parts_for(settings: MediaSettings, text: str, blocks: list[Block] | None) -> list[Part]:
    """A stimulus as parts: its blocks in order, or just its text when it has none."""
    if not blocks:
        return [text_block(text)]
    parts: list[Part] = []
    for block in blocks:
        if block["type"] == TEXT:
            parts.append(text_block(block["text"]))
        elif block["type"] == IMAGE:
            digest = block["ref"].removeprefix("sha256:")
            parts.append(image_part(await _data_url(settings, digest, block["mime_type"])))
        else:  # pragma: no cover - upload validation admits only these
            raise ValueError(f"unknown block type {block['type']!r}")
    return parts


def collapse(parts: list[Part]) -> str | list[Part]:
    """Adjacent text parts joined; one string when nothing else is left.

    Text parts are concatenated without a separator, so callers place every
    newline themselves and a text-only message is byte for byte the string
    they would have written.
    """
    merged: list[Part] = []
    for part in parts:
        if part["type"] == TEXT and merged and merged[-1]["type"] == TEXT:
            merged[-1] = text_block(merged[-1]["text"] + part["text"])
        else:
            merged.append(dict(part))
    if len(merged) == 1 and merged[0]["type"] == TEXT:
        return merged[0]["text"]
    if not merged:
        return ""
    return merged
