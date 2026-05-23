"""Human-as-a-tool assistance method.

The AI decomposes the question into subtasks, attempts to answer each itself,
and delegates only the ones it is genuinely uncertain about (confidence below
the threshold) to the human. It repeats this for up to max_rounds rounds,
incorporating human answers each time, before synthesising a final answer.

assistance_params:
    model:                LLM to use for decomposition (default: settings.llm.default_model)
    confidence_method:    "self_report" (default), "sampling", or "self_consistency"
    confidence_model:     LLM for confidence scoring (default: gemini-2.5-flash-lite)
    clustering_model:     LLM for semantic clustering, sampling method only (default: same as confidence_model)
    num_samples:          Samples per subtask, sampling method only (default: 5)
    max_rounds:           Maximum delegation rounds before forced synthesis (default: 5)
    max_subtasks:         Max subtasks to identify per round (default: 5)
    confidence_threshold: Show AI answer pre-filled below this score (default: 75, range 0–100)
"""

from __future__ import annotations

import json
import logging

from config import get_settings
from models import Question

from ...base import AssistanceMethod, InteractionStep, StepType
from ...confidence import (
    ConfidenceEstimator,
    LLMConfidenceEstimator,
    SamplingConfidenceEstimator,
    SelfConsistencyConfidenceEstimator,
)
from .decomposer import SubtaskDecomposer

logger = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = 75


class HumanAsAToolMethod(AssistanceMethod):
    def __init__(self, confidence_estimator: ConfidenceEstimator | None = None) -> None:
        self._decomposer = SubtaskDecomposer()
        self._estimator = confidence_estimator

    async def start(
        self,
        question: Question,
        params: dict,
        *,
        parent_question_text: str | None = None,
    ) -> InteractionStep:
        settings = get_settings()
        model = params.get("model") or settings.llm.decomposition_model
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

        result = await self._decomposer.start(question_text, options, max_subtasks, model)

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
                "model": model,
            },
        )

    async def advance(self, state: dict, human_input: str, params: dict) -> InteractionStep:
        settings = get_settings()
        model = state.get("model") or params.get("model") or settings.llm.decomposition_model

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

        result = await self._decomposer.advance(
            question_text,
            options,
            history,
            iteration=iteration,
            max_rounds=max_rounds,
            model=model,
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
                "model": model,
            },
        )

    def _get_estimator(self, params: dict) -> ConfidenceEstimator:
        if self._estimator is not None:
            return self._estimator
        settings = get_settings()
        method = params.get("confidence_method", "self_report")
        confidence_model = params.get("confidence_model") or settings.llm.confidence_model
        if method == "sampling":
            estimator: ConfidenceEstimator = SamplingConfidenceEstimator(
                sampling_model=confidence_model,
                clustering_model=params.get("clustering_model") or confidence_model,
                num_samples=int(params.get("num_samples", 5)),
            )
        elif method == "self_consistency":
            estimator = SelfConsistencyConfidenceEstimator(
                sampling_model=confidence_model,
                num_samples=int(params.get("num_samples", 5)),
            )
        else:
            estimator = LLMConfidenceEstimator(model=confidence_model)
        self._estimator = estimator
        return estimator

    async def _score_subtasks(
        self, question_text: str, subtasks: list[dict], params: dict
    ) -> list[dict]:
        """Return all subtasks with confidence scores merged in."""
        scores = await self._get_estimator(params).estimate_batch(question_text, subtasks)
        return [{**st, "confidence": score} for st, score in zip(subtasks, scores)]
