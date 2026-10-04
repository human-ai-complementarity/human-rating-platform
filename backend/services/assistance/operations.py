"""Business logic for the assistance endpoints.

Every call across the method boundary (start/advance) is also written to the
append-only ``assistance_events`` table: one ``request`` row for what went in
and one ``response`` row for the step that came out, with latency and outcome.
The session row keeps only the current step; the event rows keep the history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    AssistanceEvent,
    AssistanceEventDirection,
    AssistanceEventStatus,
    AssistanceSession,
)
from schemas import AssistanceStepResponse
from services.queries import (
    fetch_experiment_or_404,
    fetch_parent_question_text,
    fetch_question_or_404,
    fetch_rater_or_404,
)

from .base import InteractionStep, StepType
from .registry import get_method

logger = logging.getLogger(__name__)


def _load_json(value: str | None) -> dict:
    return json.loads(value) if value else {}


async def _fetch_session_or_404(session_id: int, db: AsyncSession) -> AssistanceSession:
    session = (
        await db.execute(select(AssistanceSession).where(AssistanceSession.id == session_id))
    ).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Assistance session not found")
    return session


def _apply_step_to_session(session: AssistanceSession, step: InteractionStep) -> None:
    session.step_type = step.type
    session.state = json.dumps(step.state) if step.state else None
    session.payload = json.dumps(step.payload) if step.payload else None
    session.is_complete = step.is_terminal
    session.updated_at = datetime.now(UTC)


def _step_to_response(session_id: int, step: InteractionStep) -> AssistanceStepResponse:
    return AssistanceStepResponse(
        session_id=session_id,
        type=step.type,
        payload=step.payload,
        is_terminal=step.is_terminal,
    )


def _restore_step(session: AssistanceSession) -> InteractionStep:
    return InteractionStep(
        type=StepType(session.step_type),
        payload=_load_json(session.payload),
        state=_load_json(session.state),
        is_terminal=session.is_complete,
    )


async def _fetch_existing_session(
    rater_id: int, question_id: int, db: AsyncSession
) -> AssistanceSession | None:
    return (
        await db.execute(
            select(AssistanceSession).where(
                AssistanceSession.rater_id == rater_id,
                AssistanceSession.question_id == question_id,
            )
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


@dataclass
class _MethodCall:
    """Outcome of one start()/advance() invocation, as the event log records it."""

    step: InteractionStep
    latency_ms: int
    status: AssistanceEventStatus
    error: str | None


async def _call_method(
    invoke: Callable[[], Awaitable[InteractionStep]],
    *,
    fallback: StepType,
    log_message: str,
    log_attributes: dict,
) -> _MethodCall:
    """Run a method call, timing it and degrading unrecoverable failures to ``fallback``.

    RuntimeError is the methods' documented "give up" signal. A TimeoutError
    escaping a method (an ``asyncio.wait_for`` deadline, say) is treated the
    same way so the rater is never handed a 500, and recorded as ``timeout``
    so it can be told apart from a provider or parsing failure.
    """
    started = time.monotonic()
    try:
        step = await invoke()
    except (RuntimeError, TimeoutError) as exc:
        latency_ms = _elapsed_ms(started)
        logger.error(log_message, exc_info=True, extra={"attributes": log_attributes})
        status = (
            AssistanceEventStatus.TIMEOUT
            if isinstance(exc, TimeoutError)
            else AssistanceEventStatus.ERROR
        )
        return _MethodCall(
            step=InteractionStep(type=fallback, is_terminal=True),
            latency_ms=latency_ms,
            status=status,
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = _elapsed_ms(started)
    if step.failure_reason:
        # The method caught its own failure and returned a degraded step.
        return _MethodCall(step, latency_ms, AssistanceEventStatus.ERROR, step.failure_reason)
    return _MethodCall(step, latency_ms, AssistanceEventStatus.OK, None)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _record_request(
    db: AsyncSession,
    *,
    session_id: int,
    step_type: StepType | str | None,
    payload: dict,
) -> None:
    db.add(
        AssistanceEvent(
            assistance_session_id=session_id,
            direction=AssistanceEventDirection.REQUEST.value,
            status=AssistanceEventStatus.OK.value,
            step_type=StepType(step_type).value if step_type is not None else None,
            payload=json.dumps(payload),
        )
    )


def _record_response(db: AsyncSession, *, session_id: int, call: _MethodCall) -> None:
    snapshot: dict = {
        "payload": call.step.payload,
        "state": call.step.state,
        "is_terminal": call.step.is_terminal,
    }
    if call.step.failure_reason:
        snapshot["failure_reason"] = call.step.failure_reason
    db.add(
        AssistanceEvent(
            assistance_session_id=session_id,
            direction=AssistanceEventDirection.RESPONSE.value,
            status=call.status.value,
            step_type=call.step.type.value,
            latency_ms=call.latency_ms,
            payload=json.dumps(snapshot),
            error=call.error,
        )
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def start_assistance(
    *,
    rater_id: int,
    question_id: int,
    db: AsyncSession,
) -> AssistanceStepResponse:
    rater, question = await asyncio.gather(
        fetch_rater_or_404(rater_id, db),
        fetch_question_or_404(question_id, db),
    )
    if not rater.is_active:
        raise HTTPException(status_code=400, detail="Rater session is not active")

    if question.experiment_id != rater.experiment_id:
        raise HTTPException(
            status_code=400, detail="Question does not belong to rater's experiment"
        )

    existing = await _fetch_existing_session(rater_id, question_id, db)
    if existing and existing.step_type not in (StepType.NONE, StepType.SKIP):
        return _step_to_response(existing.id, _restore_step(existing))
    # A NONE/SKIP session is retried from scratch. The row is reused rather
    # than deleted so the failed attempt's events stay attached to it.

    experiment = await fetch_experiment_or_404(rater.experiment_id, db)
    params = _load_json(experiment.assistance_params)

    try:
        method = get_method(experiment.assistance_method)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    parent_question_text = (
        await fetch_parent_question_text(question.parent_question_id, db)
        if question.parent_question_id is not None
        else None
    )

    call = await _call_method(
        lambda: method.start(
            question,
            params,
            parent_question_text=parent_question_text,
            experiment_system_prompt=experiment.system_prompt,
        ),
        fallback=StepType.NONE,
        log_message=(
            "Assistance start failed with unrecoverable error; continuing without assistance"
        ),
        log_attributes={
            "rater_id": rater_id,
            "question_id": question_id,
            "method": experiment.assistance_method,
        },
    )
    step = call.step

    if existing:
        assistance_session = existing
        assistance_session.method_name = experiment.assistance_method
        assistance_session.params = json.dumps(params) if params else None
        _apply_step_to_session(assistance_session, step)
    else:
        assistance_session = AssistanceSession(
            rater_id=rater_id,
            experiment_id=rater.experiment_id,
            question_id=question_id,
            method_name=experiment.assistance_method,
            params=json.dumps(params) if params else None,
            step_type=step.type,
            state=json.dumps(step.state) if step.state else None,
            payload=json.dumps(step.payload) if step.payload else None,
            is_complete=step.is_terminal,
        )
        db.add(assistance_session)
    try:
        # Flush first so the new row has an id for the events to point at.
        await db.flush()
        _record_request(
            db, session_id=assistance_session.id, step_type=None, payload={"params": params}
        )
        _record_response(db, session_id=assistance_session.id, call=call)
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent start for the same rater/question.
        await db.rollback()
        existing = await _fetch_existing_session(rater_id, question_id, db)
        if existing:
            return _step_to_response(existing.id, _restore_step(existing))
        raise
    await db.refresh(assistance_session)

    logger.info(
        "Assistance session started",
        extra={
            "attributes": {
                "session_id": assistance_session.id,
                "rater_id": rater_id,
                "question_id": question_id,
                "method": experiment.assistance_method,
                "status": call.status.value,
                "latency_ms": call.latency_ms,
            }
        },
    )

    return _step_to_response(assistance_session.id, step)


async def advance_assistance(
    *,
    rater_id: int,
    session_id: int,
    human_input: str,
    db: AsyncSession,
) -> AssistanceStepResponse:
    assistance_session = await _fetch_session_or_404(session_id, db)

    if assistance_session.rater_id != rater_id:
        raise HTTPException(status_code=403, detail="Session does not belong to rater")

    if assistance_session.is_complete:
        raise HTTPException(status_code=400, detail="Assistance session is already complete")

    params = _load_json(assistance_session.params)
    state = _load_json(assistance_session.state)

    try:
        method = get_method(assistance_session.method_name)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    experiment = await fetch_experiment_or_404(assistance_session.experiment_id, db)

    _record_request(
        db,
        session_id=session_id,
        step_type=assistance_session.step_type,
        payload={"human_input": human_input},
    )

    call = await _call_method(
        lambda: method.advance(
            state,
            human_input,
            params,
            experiment_system_prompt=experiment.system_prompt,
        ),
        fallback=StepType.SKIP,
        log_message=(
            "Assistance advance failed with unrecoverable error; skipping question for retry"
        ),
        log_attributes={
            "session_id": session_id,
            "rater_id": rater_id,
            "question_id": assistance_session.question_id,
            "method": assistance_session.method_name,
        },
    )
    step = call.step

    _apply_step_to_session(assistance_session, step)
    _record_response(db, session_id=session_id, call=call)
    await db.commit()

    logger.info(
        "Assistance session advanced",
        extra={
            "attributes": {
                "session_id": session_id,
                "step_type": step.type,
                "is_terminal": step.is_terminal,
                "status": call.status.value,
                "latency_ms": call.latency_ms,
            }
        },
    )

    return _step_to_response(session_id, step)
