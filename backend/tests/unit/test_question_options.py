"""Tests for the shared question-options parser.

One parse serves both the rater UI (which renders the list) and assistance
methods (which rank against it by position), so these cases pin the split for
every format datasets use.
"""

from __future__ import annotations

import sys
from pathlib import Path

from question_options import parse_options


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class TestParseOptions:
    def test_none_returns_empty(self):
        assert parse_options(None) == []

    def test_empty_string_returns_empty(self):
        assert parse_options("") == []

    def test_pipe_delimited(self):
        assert parse_options("Yes|No|Maybe") == ["Yes", "No", "Maybe"]

    def test_pipe_delimited_strips_whitespace(self):
        assert parse_options(" Yes | No ") == ["Yes", "No"]

    def test_pipe_delimited_ignores_empty_segments(self):
        assert parse_options("Yes||No") == ["Yes", "No"]

    def test_labeled_options_uppercase_letter_period(self):
        raw = "A. Option one\nB. Option two\nC. Option three"
        result = parse_options(raw)
        assert len(result) == 3
        assert "A. Option one" in result[0]
        assert "B. Option two" in result[1]
        assert "C. Option three" in result[2]

    def test_labeled_options_parens(self):
        raw = "(A) Option one\n(B) Option two"
        result = parse_options(raw)
        assert len(result) == 2

    def test_labeled_options_split_on_comma_boundary(self):
        # Mixed newline/comma labelling: every label starts a new option, and the
        # boundary comma is stripped. The rater sees the same three choices the
        # assistance method ranks.
        assert parse_options("A. Yes\nB. No, C. Maybe") == ["A. Yes", "B. No", "C. Maybe"]

    def test_newline_delimited(self):
        result = parse_options("option one\noption two\noption three")
        assert result == ["option one", "option two", "option three"]

    def test_comma_delimited_fallback(self):
        result = parse_options("alpha,beta,gamma")
        assert result == ["alpha", "beta", "gamma"]

    def test_single_value_returned_as_list(self):
        result = parse_options("only one option")
        assert result == ["only one option"]
