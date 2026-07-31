from __future__ import annotations

import csv
import io
import logging
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import Question, Rating, Rater
from services.queries import canonical_rating_rank_subquery, counts_toward_target
from .queries import fetch_experiment_or_404

logger = logging.getLogger(__name__)

EXPORT_COLUMNS = [
    "rating_id",
    "question_id",
    "question_text",
    "gt_answer",
    "rater_prolific_id",
    "rater_study_id",
    "rater_session_id",
    "answer",
    "confidence",
    "time_started",
    "time_submitted",
    "response_time_seconds",
    "counts_toward_target",
]


def build_export_filename(experiment_id: int) -> str:
    return f"experiment_{experiment_id}_ratings.csv"


def _resolve_batch_size(batch_size: int | None) -> int:
    # A request can override batch size for controlled experiments/tests;
    # otherwise we use the centralized config default.
    if batch_size is not None:
        return batch_size
    return get_settings().exports.stream_batch_size


def _build_export_header_chunk() -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(EXPORT_COLUMNS)
    return output.getvalue()


def _build_export_row(
    rating: Rating,
    question: Question,
    rater: Rater,
    counts_toward_target: bool,
) -> list[object]:
    response_time = (rating.time_submitted - rating.time_started).total_seconds()
    return [
        rating.id,
        question.question_id,
        question.question_text,
        question.gt_answer,
        rater.prolific_id,
        rater.study_id or "",
        rater.session_id or "",
        rating.answer,
        rating.confidence,
        rating.time_started.isoformat(),
        rating.time_submitted.isoformat(),
        round(response_time, 2),
        counts_toward_target,
    ]


async def stream_export_csv_chunks(
    *,
    experiment_id: int,
    db: AsyncSession,
    batch_size: int | None = None,
    include_preview: bool = False,
) -> AsyncIterator[str]:
    resolved_batch_size = _resolve_batch_size(batch_size)
    experiment = await fetch_experiment_or_404(experiment_id, db)

    logger.info(
        "CSV export started",
        extra={
            "attributes": {
                "experiment_id": experiment_id,
                "include_preview": include_preview,
            }
        },
    )
    yield _build_export_header_chunk()

    # Canonical ranking: first `num_ratings_per_question` per question count
    # toward the target, later ones are overshoot (flagged False so analysis can
    # truncate). Shared with the /api/v1 ratings endpoint via services.queries.
    rating_rank = canonical_rating_rank_subquery(experiment_id)

    statement = (
        select(Rating, Question, Rater, rating_rank.c.rank)
        .join(Question, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .outerjoin(rating_rank, Rating.id == rating_rank.c.rating_id)
        .where(Question.experiment_id == experiment_id)
        .order_by(Rating.id)
        .execution_options(stream_results=True, yield_per=resolved_batch_size)
    )
    if not include_preview:
        statement = statement.where(Rater.is_preview == False)  # noqa: E712
    result = await db.stream(statement)

    try:
        output = io.StringIO()
        writer = csv.writer(output)
        rows_in_chunk = 0
        total_rows = 0

        async for rating, question, rater, rank in result:
            counts = counts_toward_target(rank, experiment.num_ratings_per_question)
            writer.writerow(_build_export_row(rating, question, rater, counts))
            rows_in_chunk += 1
            total_rows += 1

            if rows_in_chunk >= resolved_batch_size:
                yield output.getvalue()
                output = io.StringIO()
                writer = csv.writer(output)
                rows_in_chunk = 0

        if rows_in_chunk:
            yield output.getvalue()

        logger.info(
            "CSV export completed",
            extra={
                "attributes": {
                    "experiment_id": experiment_id,
                    "row_count": total_rows,
                }
            },
        )
    finally:
        close_result = getattr(result, "close", None)
        if callable(close_result):
            maybe_awaitable = close_result()
            if hasattr(maybe_awaitable, "__await__"):
                await maybe_awaitable
