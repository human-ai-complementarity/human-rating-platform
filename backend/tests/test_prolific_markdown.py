from __future__ import annotations

import pytest

from services.admin.prolific_markdown import to_prolific_html


@pytest.mark.parametrize(
    "markdown,expected",
    [
        ("", ""),
        ("   \n  ", ""),
        ("hello", "<p>hello</p>"),
        ("# Title\nbody", "<h1>Title</h1><p>body</p>"),
        ("## Sub", "<h2>Sub</h2>"),
        ("para 1\n\npara 2", "<p>para 1</p><p>para 2</p>"),
        ("line a\nline b", "<p>line a<br>line b</p>"),
        ("- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
        ("* one\n* two", "<ul><li>one</li><li>two</li></ul>"),
        ("1. one\n2. two", "<ol><li>one</li><li>two</li></ol>"),
        ("**bold**", "<p><b>bold</b></p>"),
        ("__bold__", "<p><b>bold</b></p>"),
        ("*italic*", "<p><i>italic</i></p>"),
        ("~~strike~~", "<p><s>strike</s></p>"),
        ("a **b** *c*", "<p>a <b>b</b> <i>c</i></p>"),
    ],
)
def test_to_prolific_html_basic_shapes(markdown: str, expected: str) -> None:
    assert to_prolific_html(markdown) == expected


def test_html_in_description_is_escaped_not_passed_through() -> None:
    # Researchers pasting raw HTML shouldn't accidentally inject tags Prolific
    # would strip anyway — escape so they at least see what they typed.
    out = to_prolific_html("see <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_ampersand_is_escaped() -> None:
    assert to_prolific_html("a & b") == "<p>a &amp; b</p>"


def test_heading_with_trailing_body_keeps_body() -> None:
    # We don't want the body of a heading-block to silently disappear.
    out = to_prolific_html("# Title\nbody line 1\nbody line 2")
    assert out == "<h1>Title</h1><p>body line 1<br>body line 2</p>"


def test_crlf_line_endings_normalised() -> None:
    assert to_prolific_html("a\r\nb") == "<p>a<br>b</p>"
