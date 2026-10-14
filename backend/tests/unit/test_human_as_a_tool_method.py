"""Tests for HumanAsAToolMethod's LLM-error degradation paths."""

from __future__ import annotations

import sys
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


@pytest.mark.asyncio
async def test_start_reports_decomposition_cut_off_with_no_content_as_provider_error():
    # An empty reply used to parse as {} and end the session as a COMPLETE step
    # with no answer; a reply cut off at max_tokens is a provider failure.
    method = HumanAsAToolMethod()
    choice = SimpleNamespace(message=SimpleNamespace(content=""), finish_reason="length")
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[choice]))

    with (
        patch("services.assistance.llm._get_client", return_value=client),
        patch(
            "services.assistance.methods.human_as_a_tool.decomposer.get_settings",
            return_value=SimpleNamespace(llm=LLMSettings(openrouter_api_key="sk-test")),
        ),
    ):
        step = await method.start(_make_question(), {})

    assert step.type == StepType.NONE
    assert step.failure_reason == "provider_error"
