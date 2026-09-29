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
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import AssistancePreparation, AssistanceSession, QuestionAssignment
from services.rater.validators import validate_rater_session_not_over
from schemas import AssistanceStepResponse
from services.queries import (
    fetch_experiment_or_404,
    fetch_parent_question_text,
    fetch_question_or_404,
    load_json_column,
)

from .base import InteractionStep, StepType
from .registry import get_method
from .events import _call_method, _record_call, _last_human_input
from .session_values import optional_json, step_columns
from .preparation import PreparationContext, QuestionSnapshot
from .runner import PreparationRunner, CLAIM_SECONDS, EXECUTION_SECONDS
from session_policy import resolve_session_policy

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
    for name, value in step_columns(step, datetime.now(UTC)).items():
        setattr(session, name, value)
    session.turn += 1


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
# Endpoints
# ---------------------------------------------------------------------------


async def start_assistance(
    *,
    rater_id: int,
    question_id: int,
    runner: PreparationRunner | None = None,
    db: AsyncSession,
) -> AssistanceStepResponse:
    from services.rater.queue import lock_rater

    observed = await _fetch_existing_session(rater_id, question_id, db)
    observed_turn = observed.turn if observed else None
    rater = await lock_rater(rater_id, db)
    question = await fetch_question_or_404(question_id, db)
    if not rater.is_active:
        raise HTTPException(status_code=400, detail="Rater session is not active")

    if question.experiment_id != rater.experiment_id:
        raise HTTPException(
            status_code=400, detail="Question does not belong to rater's experiment"
        )

    experiment = await fetch_experiment_or_404(rater.experiment_id, db)
    await validate_rater_session_not_over(rater, db, resolve_session_policy(experiment))
    assignment = None
    if rater.queue_mode or not rater.is_preview:
        from services.rater.queue import require_assignment

        assignment = await require_assignment(rater, question_id, db)
    existing = await _fetch_existing_session(rater_id, question_id, db, lock=True)
    if existing and existing.turn != observed_turn:
        return _resume(existing)
    if existing and runner is not None:
        prepared = (
            await db.execute(
                select(AssistancePreparation.id)
                .where(
                    AssistancePreparation.rater_id == rater_id,
                    AssistancePreparation.question_id == question_id,
                    AssistancePreparation.session_start == rater.session_start,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if prepared is not None:
            return _resume(existing)
    if existing and existing.step_type not in (StepType.NONE, StepType.SKIP):
        return _resume(existing)
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

    if runner is not None and existing is None:
        params = method.preparation_params(params)
        spec = method.plan_preparation(
            PreparationContext(
                QuestionSnapshot.capture(question),
                json.dumps(params, sort_keys=True),
                parent_question_text,
                experiment.system_prompt,
            )
        )
        if spec is not None:
            policy = resolve_session_policy(experiment)
            await db.commit()
            identifier = await runner.ensure(
                rater_id=rater_id,
                question_id=question_id,
                session_start=rater.session_start,
                method_name=experiment.assistance_method,
                spec=spec,
                params=params,
                deadline_at=policy.hard_deadline(rater.session_start),
                demanded=True,
                assignment_id=assignment.id if assignment else None,
                assignment_generation=assignment.generation if assignment else None,
            )
            return _resume(await runner.wait(identifier))

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
        # The event names the failed step being retried; only the very first
        # start of a session has no prior step and gets a null here.
        retried_step_type: str | None = existing.step_type
        assistance_session.method_name = experiment.assistance_method
        assistance_session.params = json.dumps(params) if params else None
        _apply_step_to_session(assistance_session, step)
    else:
        retried_step_type = None
        assistance_session = AssistanceSession(
            rater_id=rater_id,
            experiment_id=rater.experiment_id,
            question_id=question_id,
            method_name=experiment.assistance_method,
            params=optional_json(params),
            **step_columns(step, datetime.now(UTC)),
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
    db: AsyncSession,
    turn: int | None = None,
) -> AssistanceStepResponse:
    from services.rater.queue import lock_rater, require_assignment

    async def locked_session():
        rater = await lock_rater(rater_id, db)
        if not rater.is_active:
            raise HTTPException(403, "Session expired")
        experiment = await fetch_experiment_or_404(rater.experiment_id, db)
        await validate_rater_session_not_over(rater, db, resolve_session_policy(experiment))
        session = (
            await db.execute(
                select(AssistanceSession)
                .where(AssistanceSession.id == session_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if session is None:
            raise HTTPException(404, "Assistance session not found")
        if session.rater_id != rater_id:
            raise HTTPException(403, "Session does not belong to rater")
        if rater.queue_mode or not rater.is_preview:
            await require_assignment(rater, session.question_id, db)
        return rater, experiment, session

    def response(session):
        return AssistanceStepResponse(
            session_id=session.id,
            turn=session.turn,
            type=StepType(session.step_type),
            payload=load_json_column(session.payload),
            is_terminal=session.is_complete,
        )

    rater, experiment, session = await locked_session()
    if turn is not None and turn != session.turn:
        if turn == session.turn - 1 and await _last_human_input(session_id, db) == human_input:
            return response(session)
        raise HTTPException(
            409,
            f"Assistance session is on turn {session.turn}, not {turn}; reload to see the current step",
        )
    if session.is_complete:
        raise HTTPException(400, "Assistance session is already complete")
    now = datetime.now(UTC)
    if session.advance_token and session.advance_expires_at > now:
        raise HTTPException(409, "Assistance is still processing this turn. Retry shortly.")

    token = uuid4().hex
    session.advance_token = token
    session.advance_expires_at = now + timedelta(seconds=CLAIM_SECONDS)
    generation = rater.session_start
    params = load_json_column(session.params)
    state = load_json_column(session.state)
    method = get_method(session.method_name)
    answered_step_type = session.step_type
    system_prompt = experiment.system_prompt
    await db.commit()
    try:

        async def invoke():
            async with asyncio.timeout(EXECUTION_SECONDS):
                return await method.advance(
                    state, human_input, params, experiment_system_prompt=system_prompt
                )

        call = await _call_method(
            invoke,
            fallback=StepType.SKIP,
            log_message="Assistance advance failed; skipping question",
            log_attributes={"session_id": session_id, "rater_id": rater_id},
        )
        step = call.step

        rater, _, session = await locked_session()
        if rater.session_start != generation:
            raise HTTPException(401, "Rater session was reset")
        if session.advance_token != token or session.advance_expires_at <= datetime.now(UTC):
            raise HTTPException(409, "Assistance turn ownership expired. Retry shortly.")
        _apply_step_to_session(session, step)
        _record_call(
            db,
            session_id=session_id,
            request={"human_input": human_input, "step_type": answered_step_type},
            call=call,
        )
        session.advance_token = None
        session.advance_expires_at = None
        await db.commit()
        return response(session)
    except BaseException:
        # A disconnected request can release its own claim, never a successor's.
        # Process death leaves a bounded lease that a later retry can recover.
        await db.rollback()
        await db.execute(
            update(AssistanceSession)
            .where(AssistanceSession.id == session_id, AssistanceSession.advance_token == token)
            .values(advance_token=None, advance_expires_at=None)
        )
        await db.commit()
        raise


async def prepare_assistance(*, rater_id, assignment_id, generation, runner, db):
    from services.rater.queue import speculation_enabled, lock_rater, require_assignment

    rater = await lock_rater(rater_id, db)
    if not rater.queue_mode or not speculation_enabled(rater.experiment_id) or not rater.is_active:
        raise HTTPException(409, "Preparation is not enabled")
    assignment = await db.get(QuestionAssignment, assignment_id)
    if assignment is None or assignment.rater_id != rater_id:
        raise HTTPException(404, "Assignment not found")
    await require_assignment(rater, assignment.question_id, db, active=False, generation=generation)
    experiment = await fetch_experiment_or_404(rater.experiment_id, db)
    policy = resolve_session_policy(experiment)
    if datetime.now(UTC) > policy.deadline(rater.session_start):
        raise HTTPException(403, "Session expired")
    question = await fetch_question_or_404(assignment.question_id, db)
    method = get_method(experiment.assistance_method)
    params = method.preparation_params(load_json_column(experiment.assistance_params))
    parent = (
        await fetch_parent_question_text(question.parent_question_id, db)
        if question.parent_question_id
        else None
    )
    spec = method.plan_preparation(
        PreparationContext(
            QuestionSnapshot.capture(question),
            json.dumps(params, sort_keys=True),
            parent,
            experiment.system_prompt,
        )
    )
    if spec is None:
        return {"status": "unsupported"}
    await db.commit()
    await runner.ensure(
        rater_id=rater_id,
        question_id=question.id,
        session_start=rater.session_start,
        method_name=experiment.assistance_method,
        spec=spec,
        params=params,
        deadline_at=policy.deadline(rater.session_start),
        demanded=False,
        assignment_id=assignment.id,
        assignment_generation=generation,
    )
    return {"status": "accepted"}
