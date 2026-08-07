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
    _parse_options,
    _parse_top_n_response,
    _strip_markdown_json,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


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
# Salvaging candidates from a malformed wrapper
# ---------------------------------------------------------------------------


# Verbatim from production (claude-sonnet-4-6, CulturalBench Hard). The middle
# candidate carries `"confidence">80` — a comparison instead of a value — which
# makes the enclosing object undecodable even though candidates 1 and 3 are
# intact.
MALFORMED_PRODUCTION_RESPONSE = (
    '```json\n{"candidates":['
    '{"answer":"1. TRUE, 2. TRUE, 3. FALSE, 4. FALSE","confidence":85,'
    '"rationale":"Sandwiches and empanadas are both popular hand-held foods."},'
    '{"answer":"1. FALSE, 2. TRUE, 3. FALSE, 4. FALSE","confidence">80,'
    '"rationale":"Empanadas are the most iconic Argentine hand-held food."},'
    '{"answer":"1. TRUE, 2. TRUE, 3. FALSE, 4. TRUE","confidence":75,'
    '"rationale":"Both sandwiches and empanadas are popular Argentine foods."}'
    "]}\n```"
)


class TestSalvageMalformedWrapper:
    def test_recovers_intact_candidates_around_the_malformed_one(self):
        result = _parse_top_n_response(MALFORMED_PRODUCTION_RESPONSE)
        answers = [c["answer"] for c in result["candidates"]]
        # The corrupt middle candidate is unrecoverable; the other two are not.
        assert answers == [
            "1. TRUE, 2. TRUE, 3. FALSE, 4. FALSE",
            "1. TRUE, 2. TRUE, 3. FALSE, 4. TRUE",
        ]

    def test_salvaged_candidates_survive_normalization(self):
        parsed = _parse_top_n_response(MALFORMED_PRODUCTION_RESPONSE)
        normalized = _normalize_candidates(parsed["candidates"], [], n=3)
        assert [c["rank"] for c in normalized] == [1, 2]
        assert normalized[0]["confidence"] == 85

    def test_salvages_multiple_choice_candidates(self):
        raw = (
            '{"candidates":[{"option_index":1,"confidence":90,"rationale":"a"},'
            '{"option_index":2,"confidence">50,"rationale":"b"},'
            '{"option_index":3,"confidence":20,"rationale":"c"}]}'
        )
        result = _parse_top_n_response(raw)
        assert [c["option_index"] for c in result["candidates"]] == [1, 3]

    def test_well_formed_response_is_untouched(self):
        # The salvage path must never engage for a response that parses today,
        # so an experiment that is working keeps identical behaviour.
        payload = {
            "candidates": [
                {"answer": "A", "confidence": 90, "rationale": "r"},
                {"answer": "B", "confidence": 10, "rationale": "s"},
            ]
        }
        assert _parse_top_n_response(json.dumps(payload)) == payload

    def test_still_raises_when_nothing_is_candidate_shaped(self):
        raw = '{"candidates":[{"foo":1,"bar">2}]}'
        with pytest.raises(json.JSONDecodeError):
            _parse_top_n_response(raw)


@pytest.mark.asyncio
async def test_start_displays_salvaged_candidates_instead_of_no_assistance():
    # Before salvaging, this response produced StepType.NONE and the rater saw
    # an empty assistance panel while still being able to submit a rating.
    method = TopNAssistance()
    question = _make_question(options=None, question_type="FT")

    with patch(
        "services.assistance.methods.top_n.complete",
        new=AsyncMock(return_value=MALFORMED_PRODUCTION_RESPONSE),
    ):
        step = await method.start(question, {})

    assert step.type == StepType.DISPLAY
    assert len(step.payload["candidates"]) == 2


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

    def test_deduplicates_repeated_option_index(self):
        raw = [
            {"option_index": 1, "confidence": 80, "rationale": ""},
            {"option_index": 1, "confidence": 75, "rationale": ""},
        ]
        result = _normalize_candidates(raw, self.OPTIONS, n=3)
        assert len(result) == 1

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
    # Display order is shuffled, so assert on the set rather than the sequence.
    candidates = step.payload["candidates"]
    assert len(candidates) == 2
    assert {c["answer"] for c in candidates} == {"Yes", "No"}
    assert next(c for c in candidates if c["answer"] == "Yes")["rank"] == 1
    assert step.payload["has_options"] is True


@pytest.mark.asyncio
async def test_start_shuffles_candidate_display_order():
    method = TopNAssistance()
    question = _make_question(options="A|B|C")
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 90, "rationale": ""},
            {"option_index": 2, "confidence": 70, "rationale": ""},
            {"option_index": 3, "confidence": 50, "rationale": ""},
        ]
    )

    with (
        patch(
            "services.assistance.methods.top_n.complete", new=AsyncMock(return_value=llm_payload)
        ),
        patch(
            "services.assistance.methods.top_n.random.sample",
            side_effect=lambda population, k: list(reversed(population)),
        ),
    ):
        step = await method.start(question, {})

    candidates = step.payload["candidates"]
    assert [c["answer"] for c in candidates] == ["C", "B", "A"]
    # Rank survives the shuffle so analysis can recover the model's ordering.
    assert [c["rank"] for c in candidates] == [3, 2, 1]


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
