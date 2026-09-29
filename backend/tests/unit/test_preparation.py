"""Contract tests: preparation changes timing, not method behavior."""

import json
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from models import Question
from services.assistance.base import AssistanceMethod, InteractionStep, StepType
from services.assistance.preparation import (
    PreparationContext,
    PreparationSpec,
    QuestionSnapshot,
)
from services.assistance.registry import get_method


def context():
    question = Question(
        id=7,
        experiment_id=3,
        question_id="private-id",
        question_text="Which answer?",
        options="A. One\nB. Two",
        gt_answer="Do not reveal",
        extra_data='{"private": true}',
    )
    return PreparationContext(
        question=QuestionSnapshot.capture(question),
        params_json='{"n": 2, "nested": {"n": 2}}',
        parent_question_text="Parent context",
        experiment_system_prompt="Study instructions",
    )


def test_snapshot_is_detached_and_excludes_ground_truth():
    snapshot = context()
    params = snapshot.params
    params["nested"]["n"] = 100
    assert snapshot.params["nested"]["n"] == 2
    assert "gt_answer" not in asdict(snapshot.question)
    assert "extra_data" not in asdict(snapshot.question)
    assert "question_id" not in asdict(snapshot.question)
    assert get_method("none").plan_preparation(snapshot) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["top_n", "human_as_a_tool"])
@pytest.mark.parametrize("step_type", [StepType.NONE, StepType.DISPLAY, StepType.ASK_INPUT])
async def test_full_step_round_trip_uses_existing_start_once(name, step_type):
    method = get_method(name)
    snapshot = context()
    step = InteractionStep(
        type=step_type,
        payload={"visible": [1, 2]},
        state={"private": {"round": 1}},
        is_terminal=step_type != StepType.ASK_INPUT,
    )
    method.start = AsyncMock(return_value=step)
    spec = method.plan_preparation(snapshot)
    assert spec == method.plan_preparation(snapshot)
    artifact = json.loads(json.dumps(await method.prepare(spec)))
    consumed = await method.consume_preparation(spec, artifact)
    assert consumed == step
    method.start.assert_awaited_once_with(
        snapshot.question,
        snapshot.params,
        parent_question_text=snapshot.parent_question_text,
        experiment_system_prompt=snapshot.experiment_system_prompt,
    )
    # Consumer mutation cannot mutate the durable artifact.
    consumed.state["private"]["round"] = 9
    assert artifact["state"]["private"]["round"] == 1
    with pytest.raises(ValueError, match="Incompatible"):
        await method.consume_preparation(replace(spec, version=spec.version + 1), artifact)
    with pytest.raises(ValidationError):
        await method.consume_preparation(spec, {**artifact, "is_terminal": "false"})


class PartialPreparation(AssistanceMethod):
    """Example: fetch evidence early, compose the display only on demand."""

    def plan_preparation(self, context):
        return PreparationSpec("evidence", 1, json.dumps({"text": context.question.question_text}))

    async def prepare(self, spec):
        return {"evidence": [json.loads(spec.inputs_json)["text"]]}

    async def consume_preparation(self, spec, artifact):
        return InteractionStep(
            type=StepType.DISPLAY,
            payload={"summary": "; ".join(artifact["evidence"])},
            is_terminal=True,
        )

    async def start(self, question, params, **kwargs):
        spec = self.plan_preparation(
            PreparationContext(
                question=QuestionSnapshot.capture(question),
                params_json=json.dumps(params),
                parent_question_text=kwargs.get("parent_question_text"),
                experiment_system_prompt=kwargs.get("experiment_system_prompt"),
            )
        )
        return await self.consume_preparation(spec, await self.prepare(spec))


@pytest.mark.asyncio
@pytest.mark.parametrize("question_text", ["First question?", "Different question?"])
async def test_partial_artifact_does_not_require_a_full_interaction_step(question_text):
    method = PartialPreparation()
    snapshot = replace(context(), question=replace(context().question, question_text=question_text))
    spec = method.plan_preparation(snapshot)
    artifact = await method.prepare(spec)
    assert "type" not in artifact
    prepared = await method.consume_preparation(spec, artifact)
    assert prepared.payload == {"summary": question_text}
    assert prepared == await method.start(snapshot.question, snapshot.params)


@pytest.mark.parametrize("name", ["top_n", "human_as_a_tool"])
def test_parameter_serialization_is_normalized_before_planning(name):
    method = get_method(name)
    first = replace(context(), params_json='{"b": {"y": 2, "x": 1}, "a": [1, 2]}')
    reordered = replace(context(), params_json=' { "a": [1,2], "b": {"x":1,"y":2} } ')
    assert first.params_json == reordered.params_json
    assert method.plan_preparation(first) == method.plan_preparation(reordered)
    changed = replace(context(), params_json='{"a": [2, 1], "b": {"x":1,"y":2}}')
    assert method.plan_preparation(first) != method.plan_preparation(changed)


@pytest.mark.asyncio
async def test_real_top_n_preparation_preserves_question_and_context(monkeypatch):
    complete = AsyncMock(
        return_value=json.dumps(
            {"candidates": [{"option_index": 1, "rationale": "Supported by context"}]}
        )
    )
    monkeypatch.setattr("services.assistance.methods.top_n.complete", complete)
    snapshot = replace(
        context(),
        params_json='{"assistance_models": {"top_n": "openrouter/preparation-test"}, "n": 1}',
    )
    method = get_method("top_n")
    spec = method.plan_preparation(snapshot)
    artifact = await method.prepare(spec)
    step = await method.consume_preparation(spec, artifact)
    assert step.type == StepType.DISPLAY
    assert step.is_terminal
    assert step.payload["candidates"][0]["answer"] == "A. One"
    assert step.payload["candidates"][0]["rationale"] == "Supported by context"
    complete.assert_awaited_once()
    messages = complete.call_args.args[0]
    assert snapshot.question.question_text in messages[1]["content"]
    assert snapshot.parent_question_text in messages[1]["content"]
    assert "A. One" in messages[1]["content"]
    assert "B. Two" in messages[1]["content"]
    assert snapshot.experiment_system_prompt in messages[0]["content"]
    assert "Do not reveal" not in str(messages)
    assert complete.call_args.kwargs["model"] == "openrouter/preparation-test"


@pytest.mark.asyncio
async def test_real_human_as_a_tool_preparation_preserves_question_and_context(monkeypatch):
    decomposition = AsyncMock(
        return_value=json.dumps(
            {
                "done": False,
                "subtasks": [
                    {
                        "index": 0,
                        "question": "Which evidence?",
                        "type": "free_text",
                        "my_answer": "One",
                        "evidence": "Parent context",
                    }
                ],
            }
        )
    )
    confidence = AsyncMock(return_value='{"scores": [80]}')
    monkeypatch.setattr(
        "services.assistance.methods.human_as_a_tool.decomposer.complete", decomposition
    )
    monkeypatch.setattr("services.assistance.confidence.complete", confidence)
    snapshot = replace(
        context(),
        params_json=json.dumps(
            {
                "assistance_models": {"human_as_a_tool": "openrouter/decomposition-test"},
                "confidence_model": "openrouter/confidence-test",
            }
        ),
    )
    method = get_method("human_as_a_tool")
    spec = method.plan_preparation(snapshot)
    artifact = await method.prepare(spec)
    step = await method.consume_preparation(spec, artifact)
    assert step.type == StepType.ASK_INPUT
    assert not step.is_terminal
    assert step.payload["subtasks"][0]["question"] == "Which evidence?"
    assert step.payload["subtasks"][0]["confidence"] == 80
    assert snapshot.question.question_text in step.state["question_text"]
    assert snapshot.parent_question_text in step.state["question_text"]
    decomposition.assert_awaited_once()
    confidence.assert_awaited_once()
    messages = decomposition.call_args.args[0]
    assert snapshot.question.question_text in messages[1]["content"]
    assert snapshot.parent_question_text in messages[1]["content"]
    assert "A. One" in messages[1]["content"]
    assert snapshot.experiment_system_prompt in messages[0]["content"]
    assert "Do not reveal" not in str(messages)
    scoring_messages = confidence.call_args.args[0]
    assert snapshot.question.question_text in scoring_messages[1]["content"]
    assert snapshot.parent_question_text in scoring_messages[1]["content"]
    assert decomposition.call_args.kwargs["model"] == "openrouter/decomposition-test"
    assert confidence.call_args.kwargs["model"] == "openrouter/confidence-test"


def test_runtime_identity_isolates_question_rater_and_session():
    from datetime import UTC, datetime, timedelta
    from services.assistance.runner import preparation_identity

    now = datetime.now(UTC)
    spec = PreparationSpec("evidence", 1, "{}")
    identities = {
        preparation_identity(1, 1, now, "example", spec),
        preparation_identity(2, 1, now, "example", spec),
        preparation_identity(1, 2, now, "example", spec),
        preparation_identity(1, 1, now + timedelta(seconds=1), "example", spec),
        preparation_identity(1, 1, now, "example", replace(spec, version=2)),
    }
    assert len(identities) == 5
