from __future__ import annotations

import pytest

from services.prolific_markdown import to_prolific_html


@pytest.mark.parametrize(
    "markdown,expected",
    [
        ("", ""),
        ("   \n  ", ""),
        ("hello", "<p>hello</p>"),
        ("# Title\nbody", "<h1>Title</h1><p>body</p>"),
        ("## Sub", "<h2>Sub</h2>"),
        ("para 1\n\npara 2", "<p>para 1</p><p>para 2</p>"),
        # Soft newlines become separate <p> tags rather than <br>-joined inside
        # a single <p> — Prolific strips <br>, so the <br> form silently
        # collapsed back to a single line on the study page.
        ("line a\nline b", "<p>line a</p><p>line b</p>"),
        ("- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
        ("* one\n* two", "<ul><li>one</li><li>two</li></ul>"),
        ("1. one\n2. two", "<ol><li>one</li><li>two</li></ol>"),
        ("**bold**", "<p><b>bold</b></p>"),
        ("__bold__", "<p><b>bold</b></p>"),
        ("*italic*", "<p><i>italic</i></p>"),
        ("~~strike~~", "<p><s>strike</s></p>"),
        ("a **b** *c*", "<p>a <b>b</b> <i>c</i></p>"),
        # Adjacent headings (no blank line between them) each render as their
        # own tag rather than the second one being lost as paragraph text.
        ("# A\n## B", "<h1>A</h1><h2>B</h2>"),
        ("## A\n## B", "<h2>A</h2><h2>B</h2>"),
        ("# A\n## B\nbody", "<h1>A</h1><h2>B</h2><p>body</p>"),
    ],
)
def test_to_prolific_html_basic_shapes(markdown: str, expected: str) -> None:
    assert to_prolific_html(markdown) == expected


def test_overlapping_bold_italic_falls_through_as_literal() -> None:
    # `**bold and *italic***` previously produced `<b>bold and <i>italic</b></i>`
    # — overlapping tags that Prolific's sanitiser drops. With strict bold
    # delimiters the input now falls through escaped so the researcher sees
    # exactly what they typed and can fix it.
    out = to_prolific_html("**bold and *italic***")
    assert out == "<p>**bold and *italic***</p>"
    assert "<b>" not in out and "<i>" not in out


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
    assert out == "<h1>Title</h1><p>body line 1</p><p>body line 2</p>"


def test_crlf_line_endings_normalised() -> None:
    assert to_prolific_html("a\r\nb") == "<p>a</p><p>b</p>"
