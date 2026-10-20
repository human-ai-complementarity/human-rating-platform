"""Tests for HumanAsAToolMethod's LLM-error degradation paths and its params."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest
from config import LLMSettings
from models import Question, StepType
from services.assistance.methods.human_as_a_tool import HumanAsAToolMethod


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _make_question() -> Question:
    return Question(
        id=1,
        experiment_id=1,
        question_id="q1",
        question_text="Complex question?",
        options="A|B",
        question_type="MC",
    )


def _api_conn_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://example"))


def _advance_state() -> dict:
    return {
        "question_text": "Complex question?",
        "options": "A|B",
        "iteration": 1,
        "max_rounds": 5,
        "max_subtasks": 5,
        "confidence_threshold": 75,
        "subtasks": [],
        "history": [],
        "model": None,
    }


@pytest.mark.asyncio
async def test_start_returns_none_step_on_decomposer_runtime_error():
    method = HumanAsAToolMethod()
    method._decomposer.start = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

    step = await method.start(_make_question(), {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True
    assert step.failure_reason == "provider_error"
    assert step.failure_detail == "RuntimeError: LLM unavailable"


@pytest.mark.asyncio
async def test_start_returns_none_step_on_decomposer_openai_error():
    method = HumanAsAToolMethod()
    method._decomposer.start = AsyncMock(side_effect=_api_conn_error())

    step = await method.start(_make_question(), {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True


@pytest.mark.asyncio
async def test_start_returns_none_step_on_decomposer_value_error():
    method = HumanAsAToolMethod()
    method._decomposer.start = AsyncMock(side_effect=ValueError("bad model string"))

    step = await method.start(_make_question(), {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True


@pytest.mark.asyncio
async def test_advance_returns_none_step_on_decomposer_runtime_error():
    method = HumanAsAToolMethod()
    method._decomposer.advance = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

    step = await method.advance(_advance_state(), "{}", {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True
    assert step.failure_reason == "provider_error"
    assert step.failure_detail == "RuntimeError: LLM unavailable"


@pytest.mark.asyncio
async def test_advance_returns_none_step_on_decomposer_openai_error():
    method = HumanAsAToolMethod()
    method._decomposer.advance = AsyncMock(side_effect=_api_conn_error())

    step = await method.advance(_advance_state(), "{}", {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True


@pytest.mark.asyncio
async def test_advance_returns_none_step_on_decomposer_value_error():
    method = HumanAsAToolMethod()
    method._decomposer.advance = AsyncMock(side_effect=ValueError("bad model string"))

    step = await method.advance(_advance_state(), "{}", {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True


@pytest.mark.parametrize("confidence_method", ["sampling", "self_consistency"])
@pytest.mark.parametrize(
    ("params", "expected"),
    [({}, 5), ({"num_samples": None}, 5), ({"num_samples": 3}, 3)],
    ids=["absent", "cleared", "set"],
)
def test_a_cleared_num_samples_runs_the_default(confidence_method, params, expected):
    """PATCH stores a null `num_samples` as a clear; it ran `int(None)` before."""
    params = {"confidence_method": confidence_method, **params}
    estimator = HumanAsAToolMethod()._get_estimator(params)

    assert estimator._num_samples == expected


@contextmanager
def _llm_replying(content: str | None, finish_reason: str | None):
    choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[choice]))
    with (
        patch("services.assistance.llm._get_client", return_value=client),
        patch(
            "services.assistance.methods.human_as_a_tool.decomposer.get_settings",
            return_value=SimpleNamespace(llm=LLMSettings(openrouter_api_key="sk-test")),
        ),
    ):
        yield


@pytest.mark.asyncio
async def test_start_reports_decomposition_cut_off_with_no_content_as_provider_error():
    # An empty reply used to parse as {} and end the session as a COMPLETE step
    # with no answer; a reply cut off at max_tokens is a provider failure.
    method = HumanAsAToolMethod()

    with _llm_replying("", "length"):
        step = await method.start(_make_question(), {})

    assert step.type == StepType.NONE
    assert step.failure_reason == "provider_error"
    assert step.failure_detail.startswith("EmptyTruncatedReplyError: LLM reply from ")
    assert "max_tokens=4096 with no content" in step.failure_detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [
        ("", "stop"),
        (None, "content_filter"),
        ('{"done": false, "subtasks": [{"index": 0, "question": "Which', "length"),
    ],
    ids=["empty-stop", "empty-content-filter", "cut-off-mid-json"],
)
async def test_start_reports_unparseable_decomposition_as_provider_error(content, finish_reason):
    # These replies reach the decomposer, which used to end the session as a
    # COMPLETE step with a blank answer; advance() already fails them.
    method = HumanAsAToolMethod()

    with _llm_replying(content, finish_reason):
        step = await method.start(_make_question(), {})

    assert step.type == StepType.NONE
    assert step.is_terminal is True
    assert step.failure_reason == "provider_error"
    assert step.failure_detail == "RuntimeError: LLM returned an unparseable response"
