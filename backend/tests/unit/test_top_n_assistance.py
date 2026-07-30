"""Tests for the top-N assistance method.

Covers the pure helper functions and the TopNAssistance.start() method
(with the LLM call mocked out).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest
from models import Question, StepType
from services.assistance.methods.top_n import (
    TopNAssistance,
    _clamp_top_n,
    _normalize_candidates,
    _parse_top_n_response,
    _strip_markdown_json,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


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
        raw = '```json\n{"candidates": []}\n```'
        assert _strip_markdown_json(raw) == '{"candidates": []}'

    def test_strips_plain_fence(self):
        raw = '```\n{"candidates": []}\n```'
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
# _normalize_candidates
# ---------------------------------------------------------------------------


class TestNormalizeCandidates:
    OPTIONS = ["Yes", "No", "Maybe"]

    def test_basic_normalization(self):
        raw = [{"option_index": 1, "confidence": 80, "rationale": "looks right"}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert len(result) == 1
        assert result[0]["answer"] == "Yes"
        assert result[0]["rank"] == 1
        assert result[0]["confidence"] == 80

    def test_respects_n_limit(self):
        raw = [
            {"option_index": 1, "confidence": 90, "rationale": ""},
            {"option_index": 2, "confidence": 70, "rationale": ""},
            {"option_index": 3, "confidence": 50, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=2)
        assert len(result) == 2

    def test_sorts_by_descending_confidence(self):
        raw = [
            {"option_index": 1, "confidence": 40, "rationale": ""},
            {"option_index": 2, "confidence": 90, "rationale": ""},
            {"option_index": 3, "confidence": 65, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert [c["answer"] for c in result] == ["No", "Maybe", "Yes"]
        assert [c["confidence"] for c in result] == [90, 65, 40]
        assert [c["rank"] for c in result] == [1, 2, 3]

    def test_keeps_highest_confidence_when_truncating(self):
        # The LLM returned its best candidate last; slicing before sorting would
        # have dropped it.
        raw = [
            {"option_index": 1, "confidence": 20, "rationale": ""},
            {"option_index": 2, "confidence": 30, "rationale": ""},
            {"option_index": 3, "confidence": 95, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=1)
        assert [c["answer"] for c in result] == ["Maybe"]

    def test_equal_confidence_keeps_llm_order(self):
        raw = [
            {"option_index": 3, "confidence": 50, "rationale": ""},
            {"option_index": 1, "confidence": 50, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert [c["answer"] for c in result] == ["Maybe", "Yes"]

    def test_deduplicates_repeated_option_index(self):
        raw = [
            {"option_index": 1, "confidence": 80, "rationale": ""},
            {"option_index": 1, "confidence": 75, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert len(result) == 1

    def test_dedup_keeps_the_highest_confidence_duplicate(self):
        # Dedup runs after the sort, so the weaker duplicate listed first does
        # not shadow the stronger one and cost "Yes" its place.
        raw = [
            {"option_index": 1, "confidence": 40, "rationale": "weak"},
            {"option_index": 1, "confidence": 95, "rationale": "strong"},
            {"option_index": 2, "confidence": 50, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=1)
        assert [(c["answer"], c["confidence"], c["rationale"]) for c in result] == [
            ("Yes", 95, "strong")
        ]

    def test_dedup_keeps_the_highest_confidence_duplicate_free_response(self):
        raw = [
            {"answer": "Paris", "confidence": 30, "rationale": ""},
            {"answer": "paris", "confidence": 90, "rationale": ""},
        ]
        result = _normalize_candidates(raw, [], n=3)
        assert [(c["answer"], c["confidence"]) for c in result] == [("paris", 90)]

    def test_drops_out_of_range_option_index(self):
        raw = [
            {"option_index": 0, "confidence": 90, "rationale": ""},
            {"option_index": 99, "confidence": 80, "rationale": ""},
            {"option_index": -1, "confidence": 70, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert result == []

    def test_drops_missing_or_invalid_option_index(self):
        raw = [
            {"confidence": 90, "rationale": ""},  # missing
            {"option_index": None, "confidence": 80, "rationale": ""},  # None
            {"option_index": "abc", "confidence": 70, "rationale": ""},  # non-numeric
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert result == []

    def test_accepts_string_option_index(self):
        raw = [{"option_index": "2", "confidence": 60, "rationale": ""}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert len(result) == 1
        assert result[0]["answer"] == "No"

    def test_no_options_allows_any_answer(self):
        raw = [{"answer": "Some free-form text", "confidence": 60, "rationale": "custom"}]
        result = _normalize_candidates(raw, [], n=3)
        assert len(result) == 1
        assert result[0]["answer"] == "Some free-form text"

    def test_no_options_skips_empty_answer(self):
        raw = [{"answer": "", "confidence": 80, "rationale": ""}]
        result = _normalize_candidates(raw, [], n=3)
        assert result == []

    def test_confidence_clamped_to_0_100(self):
        raw = [{"option_index": 1, "confidence": 150, "rationale": ""}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert result[0]["confidence"] == 100

        raw2 = [{"option_index": 1, "confidence": -10, "rationale": ""}]
        result2 = _normalize_candidates(raw2, self.OPTIONS, n=3)
        assert result2[0]["confidence"] == 0

    def test_invalid_confidence_defaults_to_50(self):
        raw = [{"option_index": 1, "confidence": "bad", "rationale": ""}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert result[0]["confidence"] == 50

    def test_skips_non_dict_items(self):
        raw = ["not a dict", {"option_index": 2, "confidence": 60, "rationale": ""}]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert len(result) == 1
        assert result[0]["answer"] == "No"

    def test_rank_is_sequential(self):
        raw = [
            {"option_index": 1, "confidence": 90, "rationale": ""},
            {"option_index": 2, "confidence": 70, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert [c["rank"] for c in result] == [1, 2]

    def test_non_list_raw_returns_empty(self):
        result = _normalize_candidates({"option_index": 1}, self.OPTIONS, n=3)
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
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 85, "rationale": "Strong match"},
            {"option_index": 2, "confidence": 40, "rationale": "Unlikely"},
        ]
    )

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
async def test_start_orders_candidates_by_confidence():
    method = TopNAssistance()
    question = _make_question(options="Yes|No|Maybe")
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 30, "rationale": "weak"},
            {"option_index": 3, "confidence": 88, "rationale": "strong"},
            {"option_index": 2, "confidence": 55, "rationale": "middling"},
        ]
    )

    with patch(
        "services.assistance.methods.top_n.complete", new=AsyncMock(return_value=llm_payload)
    ):
        step = await method.start(question, {"n": 3})

    candidates = step.payload["candidates"]
    assert [c["answer"] for c in candidates] == ["Maybe", "No", "Yes"]
    assert [c["rank"] for c in candidates] == [1, 2, 3]


@pytest.mark.asyncio
async def test_start_respects_n_param():
    method = TopNAssistance()
    question = _make_question(options="A|B|C|D")
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 90, "rationale": ""},
            {"option_index": 2, "confidence": 70, "rationale": ""},
            {"option_index": 3, "confidence": 50, "rationale": ""},
        ]
    )

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
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 90, "rationale": ""},
            {"option_index": 2, "confidence": 60, "rationale": ""},
        ]
    )

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
    llm_payload = _llm_response(
        [
            {"answer": "Because of X", "confidence": 75, "rationale": "compelling"},
        ]
    )

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
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 80, "rationale": "given context"},
        ]
    )

    mock_complete = AsyncMock(return_value=llm_payload)
    with patch("services.assistance.methods.top_n.complete", new=mock_complete):
        await method.start(question, {}, parent_question_text="What is the capital of France?")

    messages = mock_complete.call_args[0][0]
    user_message = next(m for m in messages if m["role"] == "user")
    assert "What is the capital of France?" in user_message["content"]


@pytest.mark.asyncio
async def test_start_returns_none_step_on_complete_runtime_error():
    method = TopNAssistance()
    question = _make_question()

    with patch(
        "services.assistance.methods.top_n.complete",
        new=AsyncMock(side_effect=RuntimeError("LLM service unavailable")),
    ):
        step = await method.start(question, {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True


@pytest.mark.asyncio
async def test_start_returns_none_step_on_openai_error():
    method = TopNAssistance()
    question = _make_question()

    api_error = openai.APIConnectionError(request=httpx.Request("POST", "https://example"))
    with patch(
        "services.assistance.methods.top_n.complete",
        new=AsyncMock(side_effect=api_error),
    ):
        step = await method.start(question, {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True


@pytest.mark.asyncio
async def test_start_returns_none_step_on_value_error():
    method = TopNAssistance()
    question = _make_question()

    with patch(
        "services.assistance.methods.top_n.complete",
        new=AsyncMock(side_effect=ValueError("Invalid model string 'gpt-4'")),
    ):
        step = await method.start(question, {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True
