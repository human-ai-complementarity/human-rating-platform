"""Business logic for the assistance endpoints.

Every call across the method boundary (start/advance) is also written to the
append-only ``assistance_events`` table: one row holding what went in, the
step that came out, latency and any error. The session row keeps only the
current step; the event rows keep the history.
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

from models import AssistanceEvent, AssistanceSession
from schemas import AssistanceStepResponse
from services.queries import (
    fetch_experiment_or_404,
    fetch_parent_question_text,
    fetch_question_or_404,
    fetch_rater_or_404,
    load_json_column,
)

from .base import InteractionStep, StepType
from .model_resolution import RESOLVED_MODEL_KEY, resolve_model
from .registry import get_method

logger = logging.getLogger(__name__)


async def _fetch_session_or_404(session_id: int, db: AsyncSession) -> AssistanceSession:
    # Row-locked for the rest of the transaction: a second advance for the same
    # session (a double-submit) waits here until the first commits, then sees
    # its result instead of racing it to the UPDATE and logging a duplicate pair.
    session = (
        await db.execute(
            select(AssistanceSession).where(AssistanceSession.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Assistance session not found")
    return session


def _apply_step_to_session(session: AssistanceSession, step: InteractionStep) -> None:
    session.step_type = step.type
    session.state = json.dumps(step.state) if step.state else None
    session.payload = json.dumps(step.payload) if step.payload else None
    session.is_complete = step.is_terminal
    session.turn += 1
    session.updated_at = datetime.now(UTC)


def _step_to_response(
    session_id: int, step: InteractionStep, *, turn: int
) -> AssistanceStepResponse:
    return AssistanceStepResponse(
        session_id=session_id,
        type=step.type,
        payload=step.payload,
        is_terminal=step.is_terminal,
        turn=turn,
    )


def _resume(session: AssistanceSession) -> AssistanceStepResponse:
    """Report the step a session is currently on, without touching it."""
    step = InteractionStep(
        type=StepType(session.step_type),
        payload=load_json_column(session.payload),
        state=load_json_column(session.state),
        is_terminal=session.is_complete,
    )
    return _step_to_response(session.id, step, turn=session.turn)


async def _fetch_existing_session(
    rater_id: int, question_id: int, db: AsyncSession, *, lock: bool = False
) -> AssistanceSession | None:
    """The rater's session for this question, if any.

    With ``lock``, the row is SELECT ... FOR UPDATE'd and its attributes
    re-read from the database once the lock is held, so a caller that waited
    behind a concurrent writer sees what that writer committed rather than
    the stale copy already in the identity map.
    """
    query = select(AssistanceSession).where(
        AssistanceSession.rater_id == rater_id,
        AssistanceSession.question_id == question_id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


@dataclass
class _MethodCall:
    """Outcome of one start()/advance() invocation, as the event log records it."""

    step: InteractionStep
    latency_ms: int
    # Exception text or the method's failure_reason; None when the call succeeded.
    error: str | None


async def _call_method(
    invoke: Callable[[], Awaitable[InteractionStep]],
    *,
    fallback: StepType,
    log_message: str,
    log_attributes: dict,
) -> _MethodCall:
    """Run a method call, timing it and degrading any failure to ``fallback``.

    RuntimeError is the methods' documented "give up" signal, but anything
    else escaping a method (a KeyError on corrupt state, a provider exception
    the method forgot to catch) is just as unrecoverable from here, and a 500
    would roll back the event row that is supposed to explain it. So every
    exception degrades to the fallback step and is logged with its traceback.
    """
    started = time.monotonic()
    try:
        step = await invoke()
    except Exception as exc:
        latency_ms = _elapsed_ms(started)
        logger.error(log_message, exc_info=True, extra={"attributes": log_attributes})
        return _MethodCall(
            step=InteractionStep(type=fallback, is_terminal=True),
            latency_ms=latency_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = _elapsed_ms(started)
    # A set failure_reason means the method caught its own failure and
    # returned a degraded step.
    return _MethodCall(step, latency_ms, step.failure_reason)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _record_call(db: AsyncSession, *, session_id: int, request: dict, call: _MethodCall) -> None:
    """Append the one event row for a start()/advance() call."""
    response: dict = {
        "payload": call.step.payload,
        "state": call.step.state,
        "is_terminal": call.step.is_terminal,
    }
    if call.step.failure_reason:
        response["failure_reason"] = call.step.failure_reason
    db.add(
        AssistanceEvent(
            assistance_session_id=session_id,
            step_type=call.step.type.value,
            latency_ms=call.latency_ms,
            payload=json.dumps({"request": request, "response": response}),
            error=call.error,
        )
    )


async def _last_human_input(session_id: int, db: AsyncSession) -> str | None:
    """The ``human_input`` of the session's most recent call, if it was an advance."""
    payload = (
        await db.execute(
            select(AssistanceEvent.payload)
            .where(AssistanceEvent.assistance_session_id == session_id)
            .order_by(AssistanceEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return load_json_column(payload).get("request", {}).get("human_input")


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
        return _resume(existing)
    if existing:
        # A NONE/SKIP session is retried from scratch. The row is reused rather
        # than deleted so the failed attempt's events stay attached to it.
        # Reusing it forfeits the unique-constraint guard a fresh INSERT would
        # have, so take the row lock instead: a concurrent retry for the same
        # rater/question blocks here until this one commits. If the row's turn
        # moved while we waited, that retry already ran the method for this
        # double-click, so report its outcome (even another failure) rather
        # than running and logging our own.
        seen_turn = existing.turn
        existing = await _fetch_existing_session(rater_id, question_id, db, lock=True)
        if existing and (
            existing.turn != seen_turn or existing.step_type not in (StepType.NONE, StepType.SKIP)
        ):
            return _resume(existing)

    experiment = await fetch_experiment_or_404(rater.experiment_id, db)
    params = load_json_column(experiment.assistance_params)

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

    # Snapshot the *resolved* model alongside the params so the session records
    # what actually ran, whether from `assistance_models` or settings.
    # Provenance the export can join against. advance() gets this snapshot
    # back as its params, so the record must not use the removed `model` key.
    # The event's request keeps the params start() was actually given.
    recorded_params = dict(params) if params else {}
    if method.primary_model_role is not None:
        recorded_params[RESOLVED_MODEL_KEY] = resolve_model(
            recorded_params, experiment.assistance_method, method.primary_model_role
        )
    recorded_params_json = json.dumps(recorded_params) if recorded_params else None

    if existing:
        assistance_session = existing
        # The event names the failed step being retried; only the very first
        # start of a session has no prior step and gets a null here.
        retried_step_type: str | None = existing.step_type
        assistance_session.method_name = experiment.assistance_method
        assistance_session.params = recorded_params_json
        _apply_step_to_session(assistance_session, step)
    else:
        retried_step_type = None
        assistance_session = AssistanceSession(
            rater_id=rater_id,
            experiment_id=rater.experiment_id,
            question_id=question_id,
            method_name=experiment.assistance_method,
            params=recorded_params_json,
            step_type=step.type,
            state=json.dumps(step.state) if step.state else None,
            payload=json.dumps(step.payload) if step.payload else None,
            is_complete=step.is_terminal,
            turn=1,
        )
        db.add(assistance_session)
    try:
        # Flush first so the new row has an id for the event to point at.
        await db.flush()
        _record_call(
            db,
            session_id=assistance_session.id,
            request={"params": params, "retried_step_type": retried_step_type},
            call=call,
        )
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent start for the same rater/question.
        await db.rollback()
        existing = await _fetch_existing_session(rater_id, question_id, db)
        if existing:
            return _resume(existing)
        raise

    logger.info(
        "Assistance session started",
        extra={
            "attributes": {
                "session_id": assistance_session.id,
                "rater_id": rater_id,
                "question_id": question_id,
                "method": experiment.assistance_method,
                "error": call.error,
                "latency_ms": call.latency_ms,
            }
        },
    )

    return _step_to_response(assistance_session.id, step, turn=assistance_session.turn)


async def advance_assistance(
    *,
    rater_id: int,
    session_id: int,
    human_input: str,
    turn: int | None = None,
    db: AsyncSession,
) -> AssistanceStepResponse:
    assistance_session = await _fetch_session_or_404(session_id, db)

    if assistance_session.rater_id != rater_id:
        raise HTTPException(status_code=403, detail="Session does not belong to rater")

    if turn is not None and turn != assistance_session.turn:
        # The client is not answering the step the session is on. If it is
        # re-sending the answer that just moved the session off the previous
        # turn (a retry or double-click that waited behind the first on the
        # row lock), hand back the step that answer produced. Anything else is
        # a stale or confused client whose input we must not silently drop.
        if turn == assistance_session.turn - 1 and (
            await _last_human_input(session_id, db) == human_input
        ):
            logger.info(
                "Assistance advance deduplicated",
                extra={"attributes": {"session_id": session_id, "turn": turn}},
            )
            return _resume(assistance_session)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Assistance session is on turn {assistance_session.turn}, not {turn}; "
                "reload to see the current step"
            ),
        )

    if assistance_session.is_complete:
        raise HTTPException(status_code=400, detail="Assistance session is already complete")

    params = load_json_column(assistance_session.params)
    state = load_json_column(assistance_session.state)

    try:
        method = get_method(assistance_session.method_name)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    experiment = await fetch_experiment_or_404(assistance_session.experiment_id, db)

    answered_step_type = assistance_session.step_type

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
    _record_call(
        db,
        session_id=session_id,
        request={"human_input": human_input, "step_type": answered_step_type},
        call=call,
    )
    await db.commit()

    logger.info(
        "Assistance session advanced",
        extra={
            "attributes": {
                "session_id": session_id,
                "step_type": step.type,
                "is_terminal": step.is_terminal,
                "error": call.error,
                "latency_ms": call.latency_ms,
            }
        },
    )

    return _step_to_response(session_id, step, turn=assistance_session.turn)
