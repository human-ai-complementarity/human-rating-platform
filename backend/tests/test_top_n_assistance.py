"""Tests for the top-N assistance method.

Covers the pure helper functions and the TopNAssistance.start() method
(with the LLM call mocked out).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import Question, StepType
from services.assistance.methods.top_n import (
    TopNAssistance,
    _clamp_top_n,
    _match_option_answer,
    _normalize_candidates,
    _parse_options,
    _parse_top_n_response,
    _strip_markdown_json,
)


# ---------------------------------------------------------------------------
# _parse_options
# ---------------------------------------------------------------------------


class TestParseOptions:
    def test_none_returns_empty(self):
        assert _parse_options(None) == []

    def test_empty_string_returns_empty(self):
        assert _parse_options("") == []

    def test_pipe_delimited(self):
        assert _parse_options("Yes|No|Maybe") == ["Yes", "No", "Maybe"]

    def test_pipe_delimited_strips_whitespace(self):
        assert _parse_options(" Yes | No ") == ["Yes", "No"]

    def test_pipe_delimited_ignores_empty_segments(self):
        assert _parse_options("Yes||No") == ["Yes", "No"]

    def test_labeled_options_uppercase_letter_period(self):
        raw = "A. Option one\nB. Option two\nC. Option three"
        result = _parse_options(raw)
        assert len(result) == 3
        assert "A. Option one" in result[0]
        assert "B. Option two" in result[1]
        assert "C. Option three" in result[2]

    def test_labeled_options_parens(self):
        raw = "(A) Option one\n(B) Option two"
        result = _parse_options(raw)
        assert len(result) == 2

    def test_newline_delimited(self):
        result = _parse_options("option one\noption two\noption three")
        assert result == ["option one", "option two", "option three"]

    def test_comma_delimited_fallback(self):
        result = _parse_options("alpha,beta,gamma")
        assert result == ["alpha", "beta", "gamma"]

    def test_single_value_returned_as_list(self):
        result = _parse_options("only one option")
        assert result == ["only one option"]


# ---------------------------------------------------------------------------
# _clamp_top_n
# ---------------------------------------------------------------------------


class TestClampTopN:
    def test_normal_value(self):
        assert _clamp_top_n(5) == 5

    def test_below_minimum_clamped_to_1(self):
        assert _clamp_top_n(0) == 1
        assert _clamp_top_n(-3) == 1

    def test_above_maximum_clamped_to_10(self):
        assert _clamp_top_n(11) == 10
        assert _clamp_top_n(100) == 10

    def test_boundary_values(self):
        assert _clamp_top_n(1) == 1
        assert _clamp_top_n(10) == 10

    def test_string_integer(self):
        assert _clamp_top_n("4") == 4

    def test_non_numeric_string_defaults_to_3(self):
        assert _clamp_top_n("bad") == 3

    def test_none_defaults_to_3(self):
        assert _clamp_top_n(None) == 3

    def test_float_truncated(self):
        assert _clamp_top_n(3.9) == 3


# ---------------------------------------------------------------------------
# _strip_markdown_json
# ---------------------------------------------------------------------------


class TestStripMarkdownJson:
    def test_plain_json_unchanged(self):
        raw = '{"candidates": []}'
        assert _strip_markdown_json(raw) == raw

    def test_strips_json_fence(self):
        raw = "```json\n{\"candidates\": []}\n```"
        assert _strip_markdown_json(raw) == '{"candidates": []}'

    def test_strips_plain_fence(self):
        raw = "```\n{\"candidates\": []}\n```"
        assert _strip_markdown_json(raw) == '{"candidates": []}'


# ---------------------------------------------------------------------------
# _parse_top_n_response
# ---------------------------------------------------------------------------


class TestParseTopNResponse:
    def test_valid_json(self):
        raw = json.dumps({"candidates": [{"answer": "A", "confidence": 90, "rationale": "r"}]})
        result = _parse_top_n_response(raw)
        assert result["candidates"][0]["answer"] == "A"

    def test_valid_json_wrapped_in_markdown(self):
        raw = "```json\n" + json.dumps({"candidates": []}) + "\n```"
        result = _parse_top_n_response(raw)
        assert result == {"candidates": []}

    def test_json_preceded_by_prose(self):
        raw = 'Here are the candidates: {"candidates": [{"answer": "B", "confidence": 70, "rationale": "x"}]}'
        result = _parse_top_n_response(raw)
        assert result["candidates"][0]["answer"] == "B"

    def test_raises_on_no_json(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_top_n_response("no json here")

    def test_raises_when_candidates_key_missing(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_top_n_response('{"something_else": []}')


# ---------------------------------------------------------------------------
# _match_option_answer
# ---------------------------------------------------------------------------


class TestMatchOptionAnswer:
    OPTIONS = ["Paris", "London", "Berlin"]

    def test_exact_match_case_insensitive(self):
        assert _match_option_answer("paris", self.OPTIONS) == "Paris"
        assert _match_option_answer("LONDON", self.OPTIONS) == "London"

    def test_numeric_index_one_based(self):
        assert _match_option_answer("1", self.OPTIONS) == "Paris"
        assert _match_option_answer("3", self.OPTIONS) == "Berlin"

    def test_letter_label(self):
        assert _match_option_answer("a", self.OPTIONS) == "Paris"
        assert _match_option_answer("b.", self.OPTIONS) == "London"
        assert _match_option_answer("c)", self.OPTIONS) == "Berlin"

    def test_no_match_returns_none(self):
        assert _match_option_answer("Tokyo", self.OPTIONS) == None

    def test_out_of_range_index_returns_none(self):
        assert _match_option_answer("5", self.OPTIONS) is None

    def test_option_with_label_prefix(self):
        options = ["A. Paris", "B. London"]
        assert _match_option_answer("A", options) == "A. Paris"
        assert _match_option_answer("b", options) == "B. London"


# ---------------------------------------------------------------------------
# _normalize_candidates
# ---------------------------------------------------------------------------


class TestNormalizeCandidates:
    OPTIONS = ["Yes", "No", "Maybe"]

    def test_basic_normalization(self):
        raw = [{"answer": "Yes", "confidence": 80, "rationale": "looks right"}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert len(result) == 1
        assert result[0]["answer"] == "Yes"
        assert result[0]["rank"] == 1
        assert result[0]["confidence"] == 80

    def test_respects_n_limit(self):
        raw = [
            {"answer": "Yes", "confidence": 90, "rationale": ""},
            {"answer": "No", "confidence": 70, "rationale": ""},
            {"answer": "Maybe", "confidence": 50, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=2)
        assert len(result) == 2

    def test_deduplicates_case_insensitively(self):
        raw = [
            {"answer": "Yes", "confidence": 80, "rationale": ""},
            {"answer": "yes", "confidence": 75, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert len(result) == 1

    def test_drops_answers_not_in_options(self):
        raw = [{"answer": "Absolutely", "confidence": 90, "rationale": ""}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert result == []

    def test_no_options_allows_any_answer(self):
        raw = [{"answer": "Some free-form text", "confidence": 60, "rationale": "custom"}]
        result = _normalize_candidates(raw, [], n=3)
        assert len(result) == 1
        assert result[0]["answer"] == "Some free-form text"

    def test_confidence_clamped_to_0_100(self):
        raw = [{"answer": "Yes", "confidence": 150, "rationale": ""}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert result[0]["confidence"] == 100

        raw2 = [{"answer": "Yes", "confidence": -10, "rationale": ""}]
        result2 = _normalize_candidates(raw2, self.OPTIONS, n=3)
        assert result2[0]["confidence"] == 0

    def test_invalid_confidence_defaults_to_50(self):
        raw = [{"answer": "Yes", "confidence": "bad", "rationale": ""}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert result[0]["confidence"] == 50

    def test_skips_non_dict_items(self):
        raw = ["not a dict", {"answer": "No", "confidence": 60, "rationale": ""}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert len(result) == 1
        assert result[0]["answer"] == "No"

    def test_skips_items_with_empty_answer(self):
        raw = [{"answer": "", "confidence": 80, "rationale": ""}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert result == []

    def test_rank_is_sequential(self):
        raw = [
            {"answer": "Yes", "confidence": 90, "rationale": ""},
            {"answer": "No", "confidence": 70, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert [c["rank"] for c in result] == [1, 2]

    def test_non_list_raw_returns_empty(self):
        result = _normalize_candidates({"answer": "Yes"}, self.OPTIONS, n=3)
        assert result == []


# ---------------------------------------------------------------------------
# TopNAssistance.start() — integration with mocked LLM
# ---------------------------------------------------------------------------


def _make_question(options: str | None = "Yes|No", question_type: str = "MC") -> Question:
    return Question(
        id=1,
        experiment_id=1,
        question_id="q1",
        question_text="Is this a good answer?",
        options=options,
        question_type=question_type,
    )


def _llm_response(candidates: list[dict]) -> str:
    return json.dumps({"candidates": candidates})


@pytest.mark.asyncio
async def test_start_returns_display_step_with_candidates():
    method = TopNAssistance()
    question = _make_question()
    llm_payload = _llm_response([
        {"answer": "Yes", "confidence": 85, "rationale": "Strong match"},
        {"answer": "No", "confidence": 40, "rationale": "Unlikely"},
    ])

    with patch(
        "services.assistance.methods.top_n.complete", new=AsyncMock(return_value=llm_payload)
    ):
        step = await method.start(question, {})

    assert step.type == StepType.DISPLAY
    assert step.is_terminal is True
    assert step.payload["kind"] == "top_n"
    assert len(step.payload["candidates"]) == 2
    assert step.payload["candidates"][0]["answer"] == "Yes"
    assert step.payload["candidates"][0]["rank"] == 1
    assert step.payload["has_options"] is True


@pytest.mark.asyncio
async def test_start_respects_n_param():
    method = TopNAssistance()
    question = _make_question(options="A|B|C|D")
    llm_payload = _llm_response([
        {"answer": "A", "confidence": 90, "rationale": ""},
        {"answer": "B", "confidence": 70, "rationale": ""},
        {"answer": "C", "confidence": 50, "rationale": ""},
    ])

    with patch(
        "services.assistance.methods.top_n.complete", new=AsyncMock(return_value=llm_payload)
    ):
        step = await method.start(question, {"n": 2})

    assert step.payload["top_n"] == 2
    assert len(step.payload["candidates"]) == 2


@pytest.mark.asyncio
async def test_start_clamps_n_to_number_of_options():
    method = TopNAssistance()
    question = _make_question(options="Yes|No")  # only 2 options
    llm_payload = _llm_response([
        {"answer": "Yes", "confidence": 90, "rationale": ""},
        {"answer": "No", "confidence": 60, "rationale": ""},
    ])

    with patch(
        "services.assistance.methods.top_n.complete", new=AsyncMock(return_value=llm_payload)
    ):
        step = await method.start(question, {"n": 5})

    # n should be clamped to 2 (number of options)
    assert step.payload["top_n"] == 2


@pytest.mark.asyncio
async def test_start_returns_none_step_on_empty_candidates():
    method = TopNAssistance()
    question = _make_question()
    llm_payload = _llm_response([])  # LLM returns no candidates

    with patch(
        "services.assistance.methods.top_n.complete", new=AsyncMock(return_value=llm_payload)
    ):
        step = await method.start(question, {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True


@pytest.mark.asyncio
async def test_start_returns_none_step_on_unparseable_llm_response():
    method = TopNAssistance()
    question = _make_question()

    with patch(
        "services.assistance.methods.top_n.complete",
        new=AsyncMock(return_value="not json at all"),
    ):
        step = await method.start(question, {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True


@pytest.mark.asyncio
async def test_start_free_response_has_options_false():
    method = TopNAssistance()
    question = _make_question(options=None, question_type="FR")
    llm_payload = _llm_response([
        {"answer": "Because of X", "confidence": 75, "rationale": "compelling"},
    ])

    with patch(
        "services.assistance.methods.top_n.complete", new=AsyncMock(return_value=llm_payload)
    ):
        step = await method.start(question, {})

    assert step.payload["has_options"] is False
    assert step.payload["candidates"][0]["answer"] == "Because of X"


@pytest.mark.asyncio
async def test_start_includes_parent_question_context():
    method = TopNAssistance()
    question = _make_question()
    llm_payload = _llm_response([
        {"answer": "Yes", "confidence": 80, "rationale": "given context"},
    ])

    mock_complete = AsyncMock(return_value=llm_payload)
    with patch("services.assistance.methods.top_n.complete", new=mock_complete):
        await method.start(question, {}, parent_question_text="What is the capital of France?")

    messages = mock_complete.call_args[0][0]
    user_message = next(m for m in messages if m["role"] == "user")
    assert "What is the capital of France?" in user_message["content"]
