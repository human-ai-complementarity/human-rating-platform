from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from database import get_session
from schemas import (
    QueueRequest,
    QueueSnapshot,
    PreparationRequest,
    AssistanceAdvanceRequest,
    AssistanceStartRequest,
    AssistanceStepResponse,
    QuestionResponse,
    RaterStartResponse,
    RatingResponse,
    RatingSubmit,
    SessionStatusResponse,
)
from services import assistance, rater

from .deps import RaterSession, require_rater_session

router = APIRouter(prefix="/raters", tags=["raters"])


@router.post("/start", response_model=RaterStartResponse)
async def start_session(
    experiment_id: int = Query(...),
    PROLIFIC_PID: str = Query(...),
    STUDY_ID: str = Query(...),
    SESSION_ID: str = Query(...),
    preview: bool = Query(False),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_session),
):
    result = await rater.start_session(
        settings=settings,
        experiment_id=experiment_id,
        prolific_pid=PROLIFIC_PID,
        study_id=STUDY_ID,
        session_id=SESSION_ID,
        is_preview=preview,
        db=db,
    )

    from models import Rater

    stored = await db.get(Rater, result.rater_id)
    result.queue_enabled = stored.queue_mode or experiment_id in settings.prefetch.experiment_ids
    return result


@router.get("/next-question", response_model=Optional[QuestionResponse])
async def get_next_question(
    request: Request,
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    question = await rater.get_next_question(rater_id=session.rater_id, db=db)
    if question is not None:
        from services.rater.queue import prepare_successors

        await prepare_successors(
            rater_id=session.rater_id, runner=request.app.state.preparation_runner, db=db
        )
    return question


@router.get("/questions/{question_id}", response_model=QuestionResponse)
async def get_question(
    request: Request,
    question_id: int,
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    question = await rater.get_question_by_id(
        rater_id=session.rater_id,
        question_id=question_id,
        db=db,
    )
    from services.rater.queue import prepare_successors

    await prepare_successors(
        rater_id=session.rater_id, runner=request.app.state.preparation_runner, db=db
    )
    return question


@router.post("/submit", response_model=RatingResponse)
async def submit_rating(
    rating: RatingSubmit,
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    return await rater.submit_rating(payload=rating, rater_id=session.rater_id, db=db)


@router.get("/session-status", response_model=SessionStatusResponse)
async def get_session_status(
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    return await rater.get_session_status(rater_id=session.rater_id, db=db)


@router.post("/end-session")
async def end_session(
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    return await rater.end_session(rater_id=session.rater_id, db=db)


@router.post("/assistance/start", response_model=AssistanceStepResponse)
async def start_assistance(
    request: Request,
    body: AssistanceStartRequest,
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    return await assistance.start_assistance(
        rater_id=session.rater_id,
        question_id=body.question_id,
        db=db,
        runner=request.app.state.preparation_runner,
    )


@router.post("/assistance/advance", response_model=AssistanceStepResponse)
async def advance_assistance(
    body: AssistanceAdvanceRequest,
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    return await assistance.advance_assistance(
        rater_id=session.rater_id,
        session_id=body.session_id,
        human_input=body.human_input,
        turn=body.turn,
        db=db,
    )


@router.post("/queue", response_model=QueueSnapshot)
async def queue_action(
    body: QueueRequest,
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    from services.rater.queue import queue_action as perform

    if session.session_generation is None:
        raise HTTPException(409, "Resume the session before using the queue")

    return await perform(rater_id=session.rater_id, body=body, db=db)


@router.post("/assistance/prepare", status_code=202)
async def prepare_assistance(
    request: Request,
    body: PreparationRequest,
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    from services.assistance.operations import prepare_assistance as prepare

    return await prepare(
        rater_id=session.rater_id,
        assignment_id=body.assignment_id,
        generation=body.generation,
        db=db,
        runner=request.app.state.preparation_runner,
    )
