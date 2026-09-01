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
    _SCHEMA_REJECTED_MODELS,
    _SYSTEM_PROMPT,
    _clamp_top_n,
    _compose_system_prompt,
    _normalize_candidates,
    _parse_options,
    _parse_top_n_response,
    _strip_markdown_json,
    _top_n_response_format,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def _clear_schema_rejected_models():
    _SCHEMA_REJECTED_MODELS.clear()
    yield
    _SCHEMA_REJECTED_MODELS.clear()


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

    def test_raises_on_comparison_instead_of_colon(self):
        # Verbatim from production (claude-sonnet-4-6, CulturalBench Hard).
        # Parser recovery was removed in favour of prompting a real integer;
        # a leftover comparison token must fail closed (no assistance), not be
        # repaired or salvaged.
        raw = (
            '```json\n{"candidates":['
            '{"answer":"1. TRUE, 2. TRUE, 3. FALSE, 4. FALSE","confidence":85,'
            '"rationale":"Sandwiches and empanadas are both popular hand-held foods."},'
            '{"answer":"1. FALSE, 2. TRUE, 3. FALSE, 4. FALSE","confidence">80,'
            '"rationale":"Empanadas are the most iconic Argentine hand-held food."},'
            '{"answer":"1. TRUE, 2. TRUE, 3. FALSE, 4. TRUE","confidence":75,'
            '"rationale":"Both sandwiches and empanadas are popular Argentine foods."}'
            "]}\n```"
        )
        with pytest.raises(json.JSONDecodeError):
            _parse_top_n_response(raw)

    def test_raises_on_truncated_wrapper(self):
        # Truncation is a separate failure from the comparison token. Salvaging
        # the intact prefix is an explicit non-goal: fail closed instead of a
        # silently shorter, less diverse shortlist.
        raw = (
            '{"candidates":[{"answer":"A","confidence":90,"rationale":"a"},'
            '{"answer":"B","confidence":'
        )
        with pytest.raises(json.JSONDecodeError):
            _parse_top_n_response(raw)


def test_compose_puts_json_contract_after_study_context():
    composed = _compose_system_prompt("Be a pirate.")
    assert composed.index("Be a pirate.") < composed.index(_SYSTEM_PROMPT)
    assert composed.endswith(_SYSTEM_PROMPT)


def test_compose_without_extra_is_the_method_prompt():
    assert _compose_system_prompt(None) == _SYSTEM_PROMPT
    assert _compose_system_prompt("   ") == _SYSTEM_PROMPT


def test_system_prompt_shows_a_concrete_confidence_integer():
    # Fallback for providers that ignore json_schema. `"confidence":0-100` in
    # the example is what led claude-sonnet-4-6 to emit `"confidence">80`.
    compact = _SYSTEM_PROMPT.replace(" ", "")
    assert '"confidence":80' in compact
    assert '"confidence":0-100' not in compact


class TestTopNResponseFormat:
    def test_multiple_choice_requires_option_index_and_integer_confidence(self):
        fmt = _top_n_response_format(multiple_choice=True, n=3, option_count=3)
        assert fmt["type"] == "json_schema"
        item = fmt["json_schema"]["schema"]["properties"]["candidates"]["items"]
        assert item["properties"]["confidence"]["type"] == "integer"
        assert "option_index" in item["required"]
        assert "answer" not in item["properties"]
        assert item["properties"]["option_index"]["maximum"] == 3
        assert fmt["json_schema"]["schema"]["properties"]["candidates"]["maxItems"] == 3

    def test_free_response_requires_answer_and_integer_confidence(self):
        fmt = _top_n_response_format(multiple_choice=False, n=2)
        item = fmt["json_schema"]["schema"]["properties"]["candidates"]["items"]
        assert "answer" in item["required"]
        assert "option_index" not in item["properties"]
        assert fmt["json_schema"]["schema"]["properties"]["candidates"]["maxItems"] == 2


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
    assert step.payload["parse_status"] == "clean"


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
    assert step.payload["parse_status"] == "no_candidates"


@pytest.mark.asyncio
async def test_start_marks_no_candidates_when_all_normalized_away():
    method = TopNAssistance()
    question = _make_question(options="Yes|No")
    llm_payload = _llm_response(
        [
            {"option_index": 99, "confidence": 90, "rationale": "hallucinated"},
        ]
    )

    with patch(
        "services.assistance.methods.top_n.complete", new=AsyncMock(return_value=llm_payload)
    ):
        step = await method.start(question, {})

    assert step.type == StepType.NONE
    assert step.payload == {"kind": "top_n", "parse_status": "no_candidates"}


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
    assert step.payload == {"kind": "top_n", "parse_status": "unparseable"}


@pytest.mark.asyncio
async def test_start_logs_unparseable_with_structured_attributes(caplog):
    method = TopNAssistance()
    question = _make_question()

    with (
        caplog.at_level("WARNING", logger="services.assistance.methods.top_n"),
        patch(
            "services.assistance.methods.top_n.complete",
            new=AsyncMock(return_value="not json at all"),
        ),
    ):
        await method.start(question, {})

    record = next(r for r in caplog.records if "Failed to parse" in r.getMessage())
    assert record.attributes["parse_status"] == "unparseable"
    assert record.attributes["question_id"] == question.id
    assert record.attributes["experiment_id"] == question.experiment_id


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
async def test_start_requests_json_schema_and_puts_study_prompt_first():
    method = TopNAssistance()
    question = _make_question()
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 80, "rationale": "given context"},
        ]
    )

    mock_complete = AsyncMock(return_value=llm_payload)
    with patch("services.assistance.methods.top_n.complete", new=mock_complete):
        await method.start(question, {"n": 2}, experiment_system_prompt="Be precise.")

    messages = mock_complete.call_args[0][0]
    system_message = next(m for m in messages if m["role"] == "system")
    assert system_message["content"].index("Be precise.") < system_message["content"].index(
        _SYSTEM_PROMPT
    )
    response_format = mock_complete.call_args.kwargs["response_format"]
    assert response_format == _top_n_response_format(multiple_choice=True, n=2, option_count=2)


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
    # Call never reached the parser, so there is no parse_status to record.
    assert step.payload == {}


def _api_status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://example")
    response = httpx.Response(status, request=request)
    cls = {
        400: openai.BadRequestError,
        404: openai.NotFoundError,
        422: openai.UnprocessableEntityError,
        500: openai.InternalServerError,
    }[status]
    return cls("rejected", response=response, body=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 422])
async def test_start_retries_without_schema_when_provider_rejects_it(status):
    method = TopNAssistance()
    question = _make_question()
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 80, "rationale": "ok"},
        ]
    )
    mock_complete = AsyncMock(side_effect=[_api_status_error(status), llm_payload])

    with patch("services.assistance.methods.top_n.complete", new=mock_complete):
        step = await method.start(question, {})

    assert step.type == StepType.DISPLAY
    assert mock_complete.call_count == 2
    assert mock_complete.call_args_list[0].kwargs["response_format"] == _top_n_response_format(
        multiple_choice=True, n=2, option_count=2
    )
    assert "response_format" not in mock_complete.call_args_list[1].kwargs


@pytest.mark.asyncio
async def test_start_skips_schema_after_model_has_been_rejected():
    method = TopNAssistance()
    question = _make_question()
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 80, "rationale": "ok"},
        ]
    )
    mock_complete = AsyncMock(side_effect=[_api_status_error(400), llm_payload, llm_payload])

    with patch("services.assistance.methods.top_n.complete", new=mock_complete):
        first = await method.start(question, {})
        second = await method.start(question, {})

    assert first.type == StepType.DISPLAY
    assert second.type == StepType.DISPLAY
    assert mock_complete.call_count == 3
    assert "response_format" not in mock_complete.call_args_list[2].kwargs


@pytest.mark.asyncio
async def test_start_does_not_retry_schema_on_server_error():
    method = TopNAssistance()
    question = _make_question()
    mock_complete = AsyncMock(side_effect=_api_status_error(500))

    with patch("services.assistance.methods.top_n.complete", new=mock_complete):
        step = await method.start(question, {})

    assert step.type == StepType.NONE
    assert mock_complete.call_count == 1


@pytest.mark.asyncio
async def test_start_does_not_remember_model_when_unconstrained_retry_fails():
    # 404 is also used for unknown model ids, which is not a schema rejection.
    # If the unconstrained retry also fails, the memo must not be poisoned.
    method = TopNAssistance()
    question = _make_question()
    llm_payload = _llm_response(
        [
            {"option_index": 1, "confidence": 80, "rationale": "ok"},
        ]
    )
    mock_complete = AsyncMock(
        side_effect=[_api_status_error(404), _api_status_error(404), llm_payload]
    )

    with patch("services.assistance.methods.top_n.complete", new=mock_complete):
        first = await method.start(question, {})
        second = await method.start(question, {})

    assert first.type == StepType.NONE
    assert second.type == StepType.DISPLAY
    assert not _SCHEMA_REJECTED_MODELS
    assert mock_complete.call_count == 3
    assert "response_format" in mock_complete.call_args_list[2].kwargs


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
