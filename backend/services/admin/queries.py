from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Question, Rating, Rater
from services.queries import (  # noqa: F401 — re-exported for backwards compat
    fetch_dataset_or_404,
    fetch_experiment_or_404,
    parent_question_ids_subquery,
)


async def fetch_ratings_for_experiment(
    experiment_id: int,
    db: AsyncSession,
    *,
    include_preview: bool = False,
) -> list[tuple[Rating, Question, Rater]]:
    stmt = (
        select(Rating, Question, Rater)
        .join(Question, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .where(Question.experiment_id == experiment_id)
    )
    if not include_preview:
        stmt = stmt.where(Rater.is_preview == False)  # noqa: E712
    return (await db.execute(stmt)).all()


async def fetch_total_questions_for_experiment(
    experiment_id: int,
    db: AsyncSession,
) -> int:
    total_questions = (
        await db.execute(
            select(func.count(Question.id))
            .where(Question.experiment_id == experiment_id)
            .where(Question.id.notin_(parent_question_ids_subquery()))
        )
    ).scalar_one()
    return int(total_questions or 0)


async def fetch_total_ratings_for_experiment(
    experiment_id: int,
    db: AsyncSession,
) -> int:
    total_ratings = (
        await db.execute(
            select(func.count(Rating.id))
            .join(Question, Rating.question_id == Question.id)
            .where(Question.experiment_id == experiment_id)
        )
    ).scalar_one()
    return int(total_ratings or 0)


async def fetch_timed_out_rater_count(
    experiment_id: int,
    db: AsyncSession,
    *,
    include_preview: bool = False,
) -> int:
    """How many of this experiment's raters were closed out by the clock.

    Counted off the rater rows rather than off ratings, because a rater who
    ran out of time before submitting anything has no ratings to be counted
    through — and that is the case worth surfacing.
    """
    stmt = select(func.count()).select_from(Rater).where(Rater.experiment_id == experiment_id)
    stmt = stmt.where(Rater.timed_out.is_(True))
    if not include_preview:
        stmt = stmt.where(Rater.is_preview.is_(False))
    return int((await db.execute(stmt)).scalar_one())
