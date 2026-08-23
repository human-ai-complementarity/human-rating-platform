from __future__ import annotations

from services.admin.exports import _utf8_size


def test_utf8_size_counts_bytes_not_characters() -> None:
    # StringIO.tell() counts characters. A documents.csv chunk budget named
    # in bytes has to count UTF-8, or a buffer of CJK/emoji parents can be
    # ~4x the intended flush size.
    text = "é文😀"
    assert len(text) == 3
    assert _utf8_size(text) == len(text.encode("utf-8"))
    assert _utf8_size(text) == 2 + 3 + 4
