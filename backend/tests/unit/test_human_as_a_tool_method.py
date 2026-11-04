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


# ---------------------------------------------------------------------------
# Content blocks: the decomposer sees the images the rater sees, every round
# ---------------------------------------------------------------------------


_PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


@pytest.fixture
def media_source(tmp_path, monkeypatch):
    import hashlib

    from config import get_settings

    digest = hashlib.sha256(_PNG).hexdigest()
    (tmp_path / digest).write_bytes(_PNG)
    monkeypatch.setattr(get_settings().media, "source_url", f"file://{tmp_path}")
    return digest


def _image_block(digest: str) -> dict:
    return {"type": "image", "ref": f"sha256:{digest}", "mime_type": "image/png"}


def test_frame_gives_the_text_form_and_no_blocks_for_text_only_questions():
    from services.assistance.methods.human_as_a_tool.method import _frame

    assert _frame("Q?", None, None) == ("Q?", None)
    assert _frame("Q?", [{"type": "text", "text": "Q?"}], "ctx") == (
        "Context:\nctx\n\nQuestion: Q?",
        None,
    )


def test_frame_mirrors_the_text_framing_with_images_in_place():
    from services.assistance.methods.human_as_a_tool.method import _frame

    image = _image_block("a" * 64)
    text, blocks = _frame("Allowed?", [{"type": "text", "text": "Screen:"}, image], "Task: do X")
    assert text == "Context:\nTask: do X\n\nQuestion: Allowed?"
    assert blocks == [
        {"type": "text", "text": "Context:\n"},
        {"type": "text", "text": "Task: do X"},
        {"type": "text", "text": "\n\nQuestion: "},
        {"type": "text", "text": "Screen:"},
        image,
    ]


@pytest.mark.asyncio
async def test_start_pins_the_stimulus_and_advance_resends_it(media_source):
    import json

    from services.assistance.methods.human_as_a_tool import decomposer as decomposer_module

    method = HumanAsAToolMethod()
    question = _make_question()
    question.content = json.dumps([{"type": "text", "text": "Screen:"}, _image_block(media_source)])
    calls: list[list[dict]] = []

    async def fake_complete(messages, **_kwargs):
        calls.append(messages)
        return json.dumps(
            {
                "done": False,
                "subtasks": [
                    {
                        "index": 0,
                        "question": "Is the dialog open?",
                        "type": "binary",
                        "options": None,
                        "my_answer": "yes",
                        "my_answer_index": None,
                        "evidence": "",
                    }
                ],
            }
        )

    with (
        patch.object(decomposer_module, "complete", new=fake_complete),
        patch.object(method, "_score_subtasks", new=AsyncMock(side_effect=lambda _q, s, _p: s)),
    ):
        step = await method.start(question, {}, parent_question_text="Task: do X")
        assert step.type == StepType.ASK_INPUT
        assert step.state["stimulus"][-1] == _image_block(media_source)
        assert step.state["question_text"] == "Context:\nTask: do X\n\nQuestion: Complex question?"

        pinned = step.state["stimulus"]
        step = await method.advance(step.state, json.dumps({"0": "no"}), {})
        assert step.type == StepType.ASK_INPUT
        assert step.state["stimulus"] == pinned

    assert len(calls) == 2
    for messages in calls:
        parts = next(m for m in messages if m["role"] == "user")["content"]
        assert [p["type"] for p in parts] == ["text", "image_url", "text"]
        assert parts[0]["text"] == "Question: Context:\nTask: do X\n\nQuestion: Screen:"
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert parts[2]["text"].startswith("\nAnswer options: A|B")
    # The second round carries the human's answers after the images.
    assert "Information gathered so far" in calls[1][1]["content"][-1]["text"]


@pytest.mark.asyncio
async def test_text_only_question_is_sent_as_the_same_string_as_before():
    from services.assistance.methods.human_as_a_tool import decomposer as decomposer_module

    method = HumanAsAToolMethod()
    captured: list[list[dict]] = []

    async def fake_complete(messages, **_kwargs):
        captured.append(messages)
        return '{"done": true, "synthesis": {"answer": "A", "reasoning": "r"}}'

    with patch.object(decomposer_module, "complete", new=fake_complete):
        step = await method.start(_make_question(), {}, parent_question_text="ctx")

    assert step.type == StepType.COMPLETE
    user = next(m for m in captured[0] if m["role"] == "user")
    assert (
        user["content"]
        == "Question: Context:\nctx\n\nQuestion: Complex question?\nAnswer options: A|B"
    )
