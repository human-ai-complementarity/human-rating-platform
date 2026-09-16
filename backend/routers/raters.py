from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from database import get_session
from schemas import (
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
    return await rater.start_session(
        settings=settings,
        experiment_id=experiment_id,
        prolific_pid=PROLIFIC_PID,
        study_id=STUDY_ID,
        session_id=SESSION_ID,
        is_preview=preview,
        db=db,
    )


@router.get("/next-question", response_model=Optional[QuestionResponse])
async def get_next_question(
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    return await rater.get_next_question(rater_id=session.rater_id, db=db)


@router.get("/questions/{question_id}", response_model=QuestionResponse)
async def get_question(
    question_id: int,
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    return await rater.get_question_by_id(
        rater_id=session.rater_id,
        question_id=question_id,
        db=db,
    )


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
    body: AssistanceStartRequest,
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
):
    return await assistance.start_assistance(
        rater_id=session.rater_id,
        question_id=body.question_id,
        db=db,
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
        db=db,
    )
