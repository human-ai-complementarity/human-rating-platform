from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ExperimentRound, Question, Rating, Rater
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
    db: AsyncSession,
) -> list[tuple[Question, int | None]]:
    rating_counts = (
        select(
            Question.id.label("question_id"),
            func.count(Rating.id).label("count"),
        )
        .outerjoin(Rating, Rating.question_id == Question.id)
        .where(Question.experiment_id == experiment_id)
        .group_by(Question.id)
        .subquery()
    )

    eligible_query = (
        select(Question, rating_counts.c.count)
        .outerjoin(rating_counts, Question.id == rating_counts.c.question_id)
        .where(Question.experiment_id == experiment_id)
        .where(Question.id.notin_(parent_question_ids_subquery()))
    )
    if rated_question_ids:
        eligible_query = eligible_query.where(Question.id.notin_(rated_question_ids))

    return (await db.execute(eligible_query)).all()


async def fetch_rater_completed_count(rater_id: int, db: AsyncSession) -> int:
    completed = (
        await db.execute(select(func.count(Rating.id)).where(Rating.rater_id == rater_id))
    ).scalar_one()
    return int(completed or 0)
