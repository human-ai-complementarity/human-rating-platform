"""Running the assistance method on a sampled question, without a rater.

`operations.start_assistance` needs a rater row, an activated assignment and
a session, and writes the assistance tables. A probe goes through the same
`start_method` call (timeout, provider context, fail-soft) but keeps the
result on the run, so analytics never has to filter it out. Multi-turn
methods are only started; no human input is invented.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from models import Experiment
from services.assistance.base import AssistanceMethod
from services.assistance.llm import SPECULATIVE_SLOTS, speculative_call
from services.assistance.registry import get_method
from services.assistance.runner import start_method

from .rater_view import RaterQuestionView

logger = logging.getLogger(__name__)

# Every call a probe makes runs as a speculative call, so the pilot check can
# hold at most the speculative slots and live raters keep the rest. The
# fan-out is paced to that same number so a probe's latency is its own call,
# not time queued behind other probes.
_PROBE_CONCURRENCY = SPECULATIVE_SLOTS


@dataclass
class AssistanceProbe:
    question_id: int
    step_type: str
    payload: dict
    failure_reason: str | None
    failure_detail: str | None
    latency_ms: int

    @property
    def succeeded(self) -> bool:
        return self.step_type not in ("none", "skip")

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "step_type": self.step_type,
            "payload": self.payload,
            "failure_reason": self.failure_reason,
            "failure_detail": self.failure_detail,
            "latency_ms": self.latency_ms,
        }


def resolve_method(experiment: Experiment) -> tuple[AssistanceMethod, dict]:
    """The method and the params a real session would record for it."""
    method = get_method(experiment.assistance_method)
    stored = json.loads(experiment.assistance_params) if experiment.assistance_params else {}
    return method, method.preparation_params(stored if isinstance(stored, dict) else {})


async def probe_all(
    experiment: Experiment,
    views: list[RaterQuestionView],
    *,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> list[AssistanceProbe]:
    """One probe per view, a few at a time. `on_progress(done, total)` is
    awaited after each probe lands."""
    method, params = resolve_method(experiment)
    semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)
    done = 0

    async def one(view: RaterQuestionView) -> AssistanceProbe:
        nonlocal done
        async with semaphore:
            token = speculative_call.set(True)
            try:
                call = await start_method(
                    method,
                    view.question,
                    params,
                    parent_question_text=view.response.parent_question_text,
                    system_prompt=experiment.system_prompt,
                    experiment_id=experiment.id,
                    question_id=view.question_id,
                    method=experiment.assistance_method,
                    pilot_check=True,
                )
            finally:
                speculative_call.reset(token)
        done += 1
        if on_progress is not None:
            try:
                await on_progress(done, len(views))
            except Exception:  # progress is cosmetic; never fail the probe for it
                logger.exception("pilot check progress callback failed")
        return AssistanceProbe(
            question_id=view.question_id,
            step_type=call.step.type.value,
            payload=call.step.payload,
            failure_reason=call.step.failure_reason,
            failure_detail=call.error,
            latency_ms=call.latency_ms,
        )

    probes = await asyncio.gather(*(one(view) for view in views))
    return list(probes)
