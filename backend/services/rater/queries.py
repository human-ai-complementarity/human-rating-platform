from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ExperimentRound, Question, QuestionAssignment, Rating, Rater
from services.queries import (  # noqa: F401 — re-exported for backwards compat
    fetch_experiment_or_404,
    fetch_parent_question_text,
    fetch_question_or_404,
    fetch_rater_or_404,
    parent_question_ids_subquery,
)


async def fetch_round_description(
    *,
    experiment_id: int,
    prolific_study_id: str,
    is_preview: bool,
    db: AsyncSession,
) -> str | None:
    """Return the description shown to the rater on the intro screen.

    For real Prolific sessions, looks up the round whose Prolific study the
    rater entered from. For preview sessions (STUDY_ID is the stub "preview"
    string), falls back to the most recent round so admins see a representative
    preview. Returns None when no round exists yet.
    """
    if is_preview:
        return (
            await db.execute(
                select(ExperimentRound.description)
                .where(ExperimentRound.experiment_id == experiment_id)
                .order_by(ExperimentRound.round_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return (
        await db.execute(
            select(ExperimentRound.description).where(
                ExperimentRound.experiment_id == experiment_id,
                ExperimentRound.prolific_study_id == prolific_study_id,
            )
        )
    ).scalar_one_or_none()


async def fetch_rated_question_ids(rater_id: int, db: AsyncSession) -> list[int]:
    return [
        question_id
        for (question_id,) in (
            await db.execute(select(Rating.question_id).where(Rating.rater_id == rater_id))
        ).all()
    ]


async def fetch_in_progress_parent_ids(rater_id: int, db: AsyncSession) -> set[int]:
    """Parent ids for which this rater has already rated at least one child.

    Used to keep sibling sub-questions together: once a rater starts a parent
    group, subsequent picks prefer remaining children of that group.
    """
    result = await db.execute(
        select(Question.parent_question_id)
        .join(Rating, Rating.question_id == Question.id)
        .where(Rating.rater_id == rater_id)
        .where(Question.parent_question_id.is_not(None))
        .distinct()
    )
    return {pid for (pid,) in result.all() if pid is not None}


async def fetch_existing_rater_for_experiment(
    *,
    prolific_id: str,
    experiment_id: int,
    db: AsyncSession,
) -> Rater | None:
    return (
        await db.execute(
            select(Rater).where(
                Rater.prolific_id == prolific_id,
                Rater.experiment_id == experiment_id,
            )
        )
    ).scalar_one_or_none()


async def fetch_existing_rating(
    *,
    rater_id: int,
    question_id: int,
    db: AsyncSession,
) -> Rating | None:
    return (
        await db.execute(
            select(Rating).where(
                Rating.rater_id == rater_id,
                Rating.question_id == question_id,
            )
        )
    ).scalar_one_or_none()


async def fetch_eligible_questions_with_counts(
    *,
    experiment_id: int,
    rated_question_ids: list[int],
    rater_id: int,
    now: datetime,
    db: AsyncSession,
) -> list[tuple[Question, int | None, int | None]]:
    """Questions this rater may still rate, as (question, committed, reserved).

    `committed` is non-preview submitted ratings; `reserved` is live
    reservations (incomplete, unexpired assignments) held by *other* raters.
    The selector needs them separately: committed counts decide which tier a
    question falls in, while reservations only influence serving priority.
    Preview ratings are excluded: they aren't real data and must not make a
    question look satisfied to the selector.
    """
    rating_counts = (
        select(
            Rating.question_id.label("question_id"),
            func.count(Rating.id).label("count"),
        )
        .join(Rater, Rating.rater_id == Rater.id)
        .where(Rater.is_preview == False)  # noqa: E712
        .group_by(Rating.question_id)
        .subquery()
    )
    assignment_counts = (
        select(
            QuestionAssignment.question_id.label("question_id"),
            func.count(QuestionAssignment.id).label("count"),
        )
        .where(QuestionAssignment.completed_at.is_(None))
        .where(QuestionAssignment.expires_at > now)
        .where(QuestionAssignment.rater_id != rater_id)
        .group_by(QuestionAssignment.question_id)
        .subquery()
    )

    eligible_query = (
        select(
            Question,
            func.coalesce(rating_counts.c.count, 0),
            func.coalesce(assignment_counts.c.count, 0),
        )
        .outerjoin(rating_counts, Question.id == rating_counts.c.question_id)
        .outerjoin(assignment_counts, Question.id == assignment_counts.c.question_id)
        .where(Question.experiment_id == experiment_id)
        .where(Question.id.notin_(parent_question_ids_subquery()))
    )
    if rated_question_ids:
        eligible_query = eligible_query.where(Question.id.notin_(rated_question_ids))

    return (await db.execute(eligible_query)).all()


async def fetch_live_assignment_for_rater(
    *,
    rater_id: int,
    now: datetime,
    db: AsyncSession,
) -> QuestionAssignment | None:
    """The rater's current unexpired, unanswered reservation, if any.

    Served-but-unanswered questions are re-served on the next request so a
    page refresh can't be used to re-roll, and the reserved slot isn't
    forgotten.
    """
    return (
        await db.execute(
            select(QuestionAssignment)
            .where(QuestionAssignment.rater_id == rater_id)
            .where(QuestionAssignment.completed_at.is_(None))
            .where(QuestionAssignment.expires_at > now)
            .limit(1)
        )
    ).scalar_one_or_none()


async def fetch_assignment_for_question(
    *,
    rater_id: int,
    question_id: int,
    db: AsyncSession,
) -> QuestionAssignment | None:
    return (
        await db.execute(
            select(QuestionAssignment).where(
                QuestionAssignment.rater_id == rater_id,
                QuestionAssignment.question_id == question_id,
            )
        )
    ).scalar_one_or_none()


async def fetch_rater_completed_count(rater_id: int, db: AsyncSession) -> int:
    completed = (
        await db.execute(select(func.count(Rating.id)).where(Rating.rater_id == rater_id))
    ).scalar_one()
    return int(completed or 0)
