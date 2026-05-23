from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import AssistanceSession, Rating, Rater
from config import Settings
from schemas import (
    QuestionResponse,
    RaterStartResponse,
    RatingResponse,
    RatingSubmit,
    SessionStatusResponse,
)
from .mappers import (
    build_question_response,
    build_rater_start_response,
    build_session_end_time,
)
from .session_token import issue_rater_session_token
from .queries import (
    fetch_eligible_questions_with_counts,
    fetch_existing_rater_for_experiment,
    fetch_existing_rating,
    fetch_experiment_or_404,
    fetch_parent_question_text,
    fetch_question_or_404,
    fetch_rated_question_ids,
    fetch_rater_completed_count,
    fetch_rater_or_404,
)
from .selectors import build_question_selection_groups, build_selected_question
from .validators import (
    validate_existing_rater_can_resume,
    validate_question_belongs_to_rater_experiment,
    validate_rating_confidence,
    validate_rater_marked_active,
    validate_rater_session_is_active,
)

logger = logging.getLogger(__name__)


def _normalize_to_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def start_session(
    *,
    settings: Settings,
    experiment_id: int,
    prolific_pid: str,
    study_id: str,
    session_id: str,
    is_preview: bool = False,
    db: AsyncSession,
) -> RaterStartResponse:
    experiment = await fetch_experiment_or_404(experiment_id, db)

    existing_rater = await fetch_existing_rater_for_experiment(
        prolific_id=prolific_pid,
        experiment_id=experiment_id,
        db=db,
    )

    if existing_rater:
        if existing_rater.is_preview:
            # Reset preview rater so they can run through the flow again from scratch
            for rating in (
                await db.execute(select(Rating).where(Rating.rater_id == existing_rater.id))
            ).scalars():
                await db.delete(rating)
            for session in (
                await db.execute(
                    select(AssistanceSession).where(AssistanceSession.rater_id == existing_rater.id)
                )
            ).scalars():
                await db.delete(session)
            existing_rater.is_active = True
            existing_rater.session_start = datetime.now(UTC)
            existing_rater.session_end = None
            await db.commit()
            await db.refresh(existing_rater)
            logger.info(
                "Preview rater reset",
                extra={
                    "attributes": {
                        "rater_id": existing_rater.id,
                        "experiment_id": experiment_id,
                    }
                },
            )
            token = issue_rater_session_token(
                settings=settings, rater_id=existing_rater.id, experiment_id=experiment_id
            )
            return build_rater_start_response(
                rater_id=existing_rater.id,
                session_start=existing_rater.session_start,
                experiment_name=experiment.name,
                completion_url=experiment.prolific_completion_url,
                rater_session_token=token,
                assistance_method=experiment.assistance_method,
            )
        validate_existing_rater_can_resume(existing_rater)
        token = issue_rater_session_token(
            settings=settings, rater_id=existing_rater.id, experiment_id=experiment_id
        )
        return build_rater_start_response(
            rater_id=existing_rater.id,
            session_start=existing_rater.session_start,
            experiment_name=experiment.name,
            completion_url=experiment.prolific_completion_url,
            rater_session_token=token,
            assistance_method=experiment.assistance_method,
        )

    rater = Rater(
        prolific_id=prolific_pid,
        study_id=study_id,
        session_id=session_id,
        experiment_id=experiment_id,
        session_start=datetime.now(UTC),
        is_active=True,
        is_preview=is_preview,
    )
    db.add(rater)
    await db.commit()
    await db.refresh(rater)

    logger.info(
        "Rater session started",
        extra={
            "attributes": {
                "rater_id": rater.id,
                "experiment_id": experiment_id,
                "prolific_pid": prolific_pid,
                "is_preview": is_preview,
            }
        },
    )

    token = issue_rater_session_token(
        settings=settings, rater_id=rater.id, experiment_id=experiment_id
    )

    return build_rater_start_response(
        rater_id=rater.id,
        session_start=rater.session_start,
        experiment_name=experiment.name,
        completion_url=experiment.prolific_completion_url,
        rater_session_token=token,
        assistance_method=experiment.assistance_method,
    )


async def get_next_question(
    *,
    rater_id: int,
    db: AsyncSession,
) -> Optional[QuestionResponse]:
    rater = await fetch_rater_or_404(rater_id, db)
    experiment = await fetch_experiment_or_404(rater.experiment_id, db)

    await validate_rater_session_is_active(rater, db)

    rated_question_ids = await fetch_rated_question_ids(rater_id, db)
    eligible_questions = await fetch_eligible_questions_with_counts(
        experiment_id=rater.experiment_id,
        rated_question_ids=rated_question_ids,
        db=db,
    )

    under_quota, at_quota = build_question_selection_groups(
        eligible_questions=eligible_questions,
        target_ratings_per_question=experiment.num_ratings_per_question,
    )
    selected = build_selected_question(
        under_quota=under_quota,
        at_quota=at_quota,
    )

    if selected is None:
        logger.warning(
            "No eligible questions found for rater",
            extra={
                "attributes": {
                    "rater_id": rater_id,
                    "experiment_id": rater.experiment_id,
                    "eligible_count": len(eligible_questions),
                }
            },
        )
        return None
    parent_text = (
        await fetch_parent_question_text(selected.parent_question_id, db)
        if selected.parent_question_id is not None
        else None
    )
    return build_question_response(selected, parent_question_text=parent_text)


async def submit_rating(
    *,
    payload: RatingSubmit,
    rater_id: int,
    db: AsyncSession,
) -> RatingResponse:
    rater = await fetch_rater_or_404(rater_id, db)
    validate_rater_marked_active(rater)

    question = await fetch_question_or_404(payload.question_id, db)
    validate_question_belongs_to_rater_experiment(
        question_experiment_id=question.experiment_id,
        rater_experiment_id=rater.experiment_id,
    )

    existing_rating = await fetch_existing_rating(
        rater_id=rater_id,
        question_id=payload.question_id,
        db=db,
    )
    if existing_rating:
        raise HTTPException(status_code=400, detail="Already rated this question")

    validate_rating_confidence(payload.confidence)

    if payload.assistance_session_id is not None:
        assistance_session = (
            await db.execute(
                select(AssistanceSession).where(
                    AssistanceSession.id == payload.assistance_session_id
                )
            )
        ).scalar_one_or_none()
        if (
            assistance_session is None
            or assistance_session.rater_id != rater_id
            or assistance_session.question_id != payload.question_id
        ):
            raise HTTPException(
                status_code=400, detail="Invalid assistance_session_id for this rater and question"
            )

    db_rating = Rating(
        question_id=payload.question_id,
        rater_id=rater_id,
        answer=payload.answer,
        confidence=payload.confidence,
        time_started=_normalize_to_utc_aware(payload.time_started),
        time_submitted=datetime.now(UTC),
        assistance_session_id=payload.assistance_session_id,
    )
    db.add(db_rating)
    await db.commit()
    await db.refresh(db_rating)

    logger.info(
        "Rating submitted",
        extra={
            "attributes": {
                "rating_id": db_rating.id,
                "rater_id": rater_id,
                "experiment_id": rater.experiment_id,
                "question_id": payload.question_id,
                "question_type": question.question_type,
            }
        },
    )

    return RatingResponse(id=db_rating.id, success=True)


async def get_session_status(
    *,
    rater_id: int,
    db: AsyncSession,
) -> SessionStatusResponse:
    rater = await fetch_rater_or_404(rater_id, db)

    time_remaining = (
        build_session_end_time(rater.session_start) - datetime.now(UTC)
    ).total_seconds()
    if time_remaining <= 0:
        rater.is_active = False
        rater.session_end = datetime.now(UTC)
        await db.commit()
        time_remaining = 0

    completed = await fetch_rater_completed_count(rater_id, db)

    return SessionStatusResponse(
        is_active=rater.is_active,
        time_remaining_seconds=max(0, int(time_remaining)),
        questions_completed=completed,
    )


async def end_session(
    *,
    rater_id: int,
    db: AsyncSession,
) -> dict[str, str]:
    rater = await fetch_rater_or_404(rater_id, db)

    rater.is_active = False
    rater.session_end = datetime.now(UTC)
    await db.commit()

    return {"message": "Session ended successfully"}
