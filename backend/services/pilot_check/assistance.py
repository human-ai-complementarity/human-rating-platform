"""Running the assistance method on a sampled question, without a rater.

`operations.start_assistance` needs a rater row, an activated assignment and
a session, and writes the assistance tables. The check calls the method the
same way that path does (`get_method`, `preparation_params`, `start`) but
keeps the result on the run, so analytics never has to filter it out.
Multi-turn methods are only started; no human input is invented.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from models import Experiment
from services.assistance.base import AssistanceMethod, exception_text
from services.assistance.registry import get_method

from .rater_view import RaterQuestionView

logger = logging.getLogger(__name__)

# Bounded so a check cannot take every provider slot from live raters.
_PROBE_CONCURRENCY = 4


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


async def probe_question(
    method: AssistanceMethod,
    view: RaterQuestionView,
    params: dict,
    *,
    system_prompt: str | None,
    timeout_seconds: float,
) -> AssistanceProbe:
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout_seconds):
            step = await method.start(
                view.question,
                params,
                parent_question_text=view.response.parent_question_text,
                experiment_system_prompt=system_prompt,
            )
    except TimeoutError:
        return AssistanceProbe(
            question_id=view.question_id,
            step_type="none",
            payload={},
            failure_reason="execution_error",
            failure_detail=f"timed out after {timeout_seconds:.0f}s",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # a method that lets anything escape is a finding, not a 500
        logger.exception(
            "pilot check: method start() raised",
            extra={"attributes": {"question_id": view.question_id}},
        )
        return AssistanceProbe(
            question_id=view.question_id,
            step_type="none",
            payload={},
            failure_reason="execution_error",
            failure_detail=exception_text(exc),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    return AssistanceProbe(
        question_id=view.question_id,
        step_type=step.type.value,
        payload=step.payload,
        failure_reason=step.failure_reason,
        failure_detail=step.failure_detail,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


ProgressCallback = Callable[[int, int], Awaitable[None]]


async def probe_all(
    experiment: Experiment,
    views: list[RaterQuestionView],
    *,
    timeout_seconds: float,
    on_progress: ProgressCallback | None = None,
) -> tuple[list[AssistanceProbe], dict]:
    """One probe per view, a few at a time. Returns the probes and the
    resolved params, so the report can say what ran. `on_progress(done, total)`
    is awaited after each probe lands."""
    method, params = resolve_method(experiment)
    semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)
    done = 0

    async def one(view: RaterQuestionView) -> AssistanceProbe:
        nonlocal done
        async with semaphore:
            probe = await probe_question(
                method,
                view,
                params,
                system_prompt=experiment.system_prompt,
                timeout_seconds=timeout_seconds,
            )
        done += 1
        if on_progress is not None:
            try:
                await on_progress(done, len(views))
            except Exception:  # progress is cosmetic; never fail the probe for it
                logger.exception("pilot check progress callback failed")
        return probe

    probes = await asyncio.gather(*(one(view) for view in views))
    return list(probes), params
