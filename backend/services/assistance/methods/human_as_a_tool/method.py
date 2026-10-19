"""Human-as-a-tool assistance method.

The AI decomposes the question into subtasks, attempts to answer each itself,
and delegates only the ones it is genuinely uncertain about (confidence below
the threshold) to the human. It repeats this for up to max_rounds rounds,
incorporating human answers each time, before synthesising a final answer.

assistance_params:
    assistance_models:    {"human_as_a_tool": LLM for every call: decomposition,
                          confidence scoring and clustering (#148)}
                          (default per call: settings.llm.decomposition_model,
                          .confidence_model or .clustering_model)
    confidence_method:    "self_report" (default), "sampling", or "self_consistency"
    confidence_model:     Override: LLM for confidence scoring (default: the map entry)
    clustering_model:     Override: LLM for semantic clustering, sampling method only
                          (default: the map entry; independent of confidence_model)
    num_samples:          Samples per subtask, sampling method only (default: 5)
    max_rounds:           Maximum delegation rounds before forced synthesis (default: 5)
    max_subtasks:         Max subtasks to identify per round (default: 5)
    confidence_threshold: Show AI answer pre-filled below this score (default: 75, range 0–100)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import replace

import openai

from models import Question

from ...base import InteractionStep, StepType, exception_text
from ...preparation import InitialStepPreparation, QuestionSnapshot
from ...model_resolution import AssistanceModel
from ...confidence import (
    CLUSTERING_TEMPERATURE,
    SAMPLING_TEMPERATURE,
    ConfidenceEstimator,
    LLMConfidenceEstimator,
    SamplingConfidenceEstimator,
    SelfConsistencyConfidenceEstimator,
)
from .decomposer import SubtaskDecomposer

logger = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = 75
_EVIDENCE_EMPTY_CONFIDENCE_PENALTY = 20


class HumanAsAToolMethod(InitialStepPreparation):
    method_key = "human_as_a_tool"
    primary_model_role = "decomposition"

    rater_instructions = (
        "For each question, an AI will break it down into smaller subtasks. "
        "Subtasks the AI is confident about will be pre-filled with its answer — "
        "you can edit any of these if you disagree. You'll be asked to answer the "
        "subtasks the AI is uncertain about. Once all subtasks are resolved, the "
        "AI will use them to synthesise a final answer that you can review and "
        "adjust before submitting."
    )

    @classmethod
    def model_roles(cls, params: Mapping[str, object]) -> tuple[str, ...]:
        """Decomposition and confidence; clustering only for the sampling
        estimator, the one that clusters."""
        roles = (cls.primary_model_role, "confidence")
        return (*roles, "clustering") if _confidence_method(params) == "sampling" else roles

    @classmethod
    def role_models(cls, params: Mapping[str, object]) -> dict[str, AssistanceModel]:
        """The instruments' options are the estimator's, not the arm's: the
        sampling estimators draw at `SAMPLING_TEMPERATURE`, clustering groups
        at `CLUSTERING_TEMPERATURE`, self-report sends none. Filled in here,
        from the same values `_get_estimator` passes on, so the session's
        `role_models` records what runs."""
        models = super().role_models(params)
        if _confidence_method(params) in ("sampling", "self_consistency"):
            models["confidence"] = replace(models["confidence"], temperature=SAMPLING_TEMPERATURE)
        if "clustering" in models:
            models["clustering"] = replace(models["clustering"], temperature=CLUSTERING_TEMPERATURE)
        return models

    def preparation_params(self, params: dict) -> dict:
        models = self.role_models(params)
        prepared = {
            **params,
            "assistance_models": {
                **(params.get("assistance_models") or {}),
                self.method_key: models[self.primary_model_role].to_dict(),
            },
            "confidence_model": models["confidence"].model,
        }
        if "clustering" in models:
            prepared["clustering_model"] = models["clustering"].model
        return prepared

    def __init__(self, confidence_estimator: ConfidenceEstimator | None = None) -> None:
        self._decomposer = SubtaskDecomposer()
        self._estimator = confidence_estimator

    async def start(
        self,
        question: Question | QuestionSnapshot,
        params: dict,
        *,
        parent_question_text: str | None = None,
        experiment_system_prompt: str | None = None,
    ) -> InteractionStep:
        model = self.role_models(params)[self.primary_model_role]
        max_rounds = int(params.get("max_rounds", 5))
        max_subtasks = int(params.get("max_subtasks", 5))
        confidence_threshold = int(params.get("confidence_threshold", _CONFIDENCE_THRESHOLD))

        # When the row is a sub-question, prepend the parent's text as Context so the
        # LLM sees the same framing the rater does. We bake this into the effective
        # question_text once here so it flows into decomposer.start, _score_subtasks,
        # and into state for advance() to reuse.
        if parent_question_text:
            question_text = (
                f"Context:\n{parent_question_text}\n\nQuestion: {question.question_text}"
            )
        else:
            question_text = question.question_text
        options = question.options or ""

        try:
            result = await self._decomposer.start(
                question_text,
                options,
                max_subtasks,
                model,
                experiment_system_prompt=experiment_system_prompt,
            )

            if result.done:
                return InteractionStep(
                    type=StepType.COMPLETE,
                    payload={
                        "history": [],
                        "synthesis": {
                            "answer": result.synthesis.get("answer", ""),
                            "reasoning": result.synthesis.get("reasoning", ""),
                        },
                    },
                    is_terminal=True,
                )

            subtasks = await self._score_subtasks(question_text, result.subtasks, params)
        except (RuntimeError, ValueError, openai.OpenAIError) as exc:
            logger.exception("human_as_a_tool start() failed; returning no-assistance step")
            return InteractionStep(
                type=StepType.NONE,
                is_terminal=True,
                failure_reason="provider_error",
                failure_detail=exception_text(exc),
            )

        return InteractionStep(
            type=StepType.ASK_INPUT,
            payload={
                "subtasks": subtasks,
                "iteration": 1,
                "max_rounds": max_rounds,
                "confidence_threshold": confidence_threshold,
                "history": [],
            },
            state={
                "question_text": question_text,
                "options": options,
                "iteration": 1,
                "max_rounds": max_rounds,
                "max_subtasks": max_subtasks,
                "confidence_threshold": confidence_threshold,
                "subtasks": subtasks,
                "history": [],
                # Pinned for the session: a PATCH mid-session changes nothing.
                "model": model.to_dict(),
            },
        )

    async def advance(
        self,
        state: dict,
        human_input: str,
        params: dict,
        *,
        experiment_system_prompt: str | None = None,
    ) -> InteractionStep:
        # `state` first: the model is pinned at session start so a multi-turn
        # interaction cannot switch models mid-flight. A session started before
        # entries carried options pinned a bare id; it keeps running with none.
        pinned = state.get("model")
        model = (
            AssistanceModel.from_entry(pinned)
            if pinned
            else self.role_models(params)[self.primary_model_role]
        )

        try:
            raw_input: dict = json.loads(human_input)
        except json.JSONDecodeError:
            logger.warning("Failed to parse human_input as JSON: %r", human_input)
            raw_input = {}

        # Normalize: values can be plain strings (legacy) or {answer, confidence} dicts
        answers: dict[str, dict] = {
            k: v if isinstance(v, dict) else {"answer": str(v)} for k, v in raw_input.items()
        }

        iteration = state.get("iteration", 1)
        max_rounds = state.get("max_rounds", 5)
        max_subtasks = state.get("max_subtasks", 5)
        confidence_threshold = state.get("confidence_threshold", _CONFIDENCE_THRESHOLD)
        question_text = state.get("question_text", "")
        options = state.get("options", "")

        history = [
            *state.get("history", []),
            {"subtasks": state.get("subtasks", []), "answers": answers},
        ]

        try:
            result = await self._decomposer.advance(
                question_text,
                options,
                history,
                iteration=iteration,
                max_rounds=max_rounds,
                model=model,
                experiment_system_prompt=experiment_system_prompt,
            )

            if result.done:
                return InteractionStep(
                    type=StepType.COMPLETE,
                    payload={
                        "history": history,
                        "synthesis": {
                            "answer": result.synthesis.get("answer", ""),
                            "reasoning": result.synthesis.get("reasoning", ""),
                        },
                    },
                    is_terminal=True,
                )

            subtasks = await self._score_subtasks(question_text, result.subtasks, params)
        except (RuntimeError, ValueError, openai.OpenAIError) as exc:
            logger.exception("human_as_a_tool advance() failed; returning no-assistance step")
            return InteractionStep(
                type=StepType.NONE,
                is_terminal=True,
                failure_reason="provider_error",
                failure_detail=exception_text(exc),
            )

        return InteractionStep(
            type=StepType.ASK_INPUT,
            payload={
                "subtasks": subtasks,
                "iteration": iteration + 1,
                "max_rounds": max_rounds,
                "confidence_threshold": confidence_threshold,
                "history": history,
            },
            state={
                "question_text": question_text,
                "options": options,
                "iteration": iteration + 1,
                "max_rounds": max_rounds,
                "max_subtasks": max_subtasks,
                "confidence_threshold": confidence_threshold,
                "subtasks": subtasks,
                "history": history,
                "model": model.to_dict(),
            },
        )

    def _get_estimator(self, params: dict) -> ConfidenceEstimator:
        if self._estimator is not None:
            return self._estimator
        method = _confidence_method(params)
        # The map the session records: a role `model_roles` omits is a
        # KeyError here, not a model that runs unrecorded.
        models = self.role_models(params)
        # Model and temperature from the recorded entries (`role_models`), so
        # the estimator runs exactly what the session says it did.
        confidence = models["confidence"]
        if method == "sampling":
            clustering = models["clustering"]
            estimator: ConfidenceEstimator = SamplingConfidenceEstimator(
                sampling_model=confidence.model,
                clustering_model=clustering.model,
                num_samples=int(params.get("num_samples", 5)),
                temperature=confidence.temperature,
                clustering_temperature=clustering.temperature,
            )
        elif method == "self_consistency":
            estimator = SelfConsistencyConfidenceEstimator(
                sampling_model=confidence.model,
                num_samples=int(params.get("num_samples", 5)),
                temperature=confidence.temperature,
            )
        else:
            estimator = LLMConfidenceEstimator(model=confidence.model)
        self._estimator = estimator
        return estimator

    async def _score_subtasks(
        self, question_text: str, subtasks: list[dict], params: dict
    ) -> list[dict]:
        """Return all subtasks with confidence scores merged in."""
        scores = await self._get_estimator(params).estimate_batch(question_text, subtasks)
        return [
            {**st, "confidence": _apply_evidence_penalty(st, int(score))}
            for st, score in zip(subtasks, scores)
        ]


def _confidence_method(params: Mapping[str, object]) -> object:
    return params.get("confidence_method", "self_report")


def _apply_evidence_penalty(subtask: dict, raw_score: int) -> int:
    if "evidence" not in subtask:
        return raw_score
    ev = subtask.get("evidence")
    if ev is None or (isinstance(ev, str) and not ev.strip()):
        return max(0, raw_score - _EVIDENCE_EMPTY_CONFIDENCE_PENALTY)
    return raw_score
