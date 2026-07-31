from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Question, Rater, Rating
from services.queries import (
    canonical_rating_rank_subquery,
    counts_toward_target,
    fetch_experiment_or_404,
)


def _serialize_rating(
    rating: Rating,
    question: Question,
    rater: Rater,
    counts_toward_target: bool,
) -> dict[str, Any]:
    response_time = (rating.time_submitted - rating.time_started).total_seconds()
    return {
        "rating_id": rating.id,
        "question_id": question.question_id,
        "question_db_id": question.id,
        "question_text": question.question_text,
        "gt_answer": question.gt_answer,
        "options": question.options,
        "question_type": question.question_type,
        "rater_prolific_id": rater.prolific_id,
        "rater_study_id": rater.study_id,
        "rater_session_id": rater.session_id,
        "is_preview": rater.is_preview,
        "answer": rating.answer,
        "confidence": rating.confidence,
        "time_started": rating.time_started,
        "time_submitted": rating.time_submitted,
        "response_time_seconds": round(response_time, 2),
        "counts_toward_target": counts_toward_target,
    }


async def list_experiment_ratings(
    experiment_id: int,
    db: AsyncSession,
    *,
    limit: int,
    offset: int,
    include_preview: bool = False,
) -> dict[str, Any]:
    """Return one page of an experiment's raw human ratings as plain dicts.

    JSON equivalent of the CSV export, with full (untruncated) question text and
    ground truth, intended for programmatic clients (e.g. feeding an inference
    pipeline). ``total`` is the full match count so callers can page to the end.
    """
    experiment = await fetch_experiment_or_404(experiment_id, db)

    count_stmt = (
        select(func.count(Rating.id))
        .join(Question, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .where(Question.experiment_id == experiment_id)
    )
    if not include_preview:
        count_stmt = count_stmt.where(Rater.is_preview == False)  # noqa: E712
    total = int((await db.execute(count_stmt)).scalar_one() or 0)

    rating_rank = canonical_rating_rank_subquery(experiment_id)
    stmt = (
        select(Rating, Question, Rater, rating_rank.c.rank)
        .join(Question, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .outerjoin(rating_rank, Rating.id == rating_rank.c.rating_id)
        .where(Question.experiment_id == experiment_id)
        .order_by(Rating.id)
        .offset(offset)
        .limit(limit)
    )
    if not include_preview:
        stmt = stmt.where(Rater.is_preview == False)  # noqa: E712

    rows = (await db.execute(stmt)).all()
    ratings = [
        _serialize_rating(
            rating,
            question,
            rater,
            counts_toward_target(rank, experiment.num_ratings_per_question),
        )
        for rating, question, rater, rank in rows
    ]

    return {
        "experiment_id": experiment_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "ratings": ratings,
    }
