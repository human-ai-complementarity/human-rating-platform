from __future__ import annotations

import csv
import io
import logging
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from config import get_settings
from models import Question, Rating, Rater
from services.queries import canonical_rating_rank_subquery, counts_toward_target
from .queries import fetch_experiment_or_404

logger = logging.getLogger(__name__)

# Ratings stay narrow: the document lives in documents.csv, once per parent.
# Repeating it on every rating row is how a 380MB longbench becomes a 1GB CSV
# and a 500MB StringIO chunk — the same row-count batching trap that OOM-killed
# inserts of these documents.
#
# question_id strings are not unique within an experiment, so the join key
# across the two files is the numeric row id (questions.id), not the string.
EXPORT_COLUMNS = [
    "rating_id",
    "question_id",
    "question_text",
    "parent_question_id",
    "parent_row_id",
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

DOCUMENT_EXPORT_COLUMNS = ["row_id", "question_id", "question_text"]


def build_export_filename(experiment_id: int) -> str:
    return f"experiment_{experiment_id}_ratings.csv"


def build_documents_export_filename(experiment_id: int) -> str:
    return f"experiment_{experiment_id}_documents.csv"


def _resolve_batch_size(batch_size: int | None) -> int:
    # A request can override batch size for controlled experiments/tests;
    # otherwise we use the centralized config default.
    if batch_size is not None:
        return batch_size
    return get_settings().exports.stream_batch_size


def _document_chunk_max_bytes() -> int:
    return get_settings().exports.stream_chunk_max_bytes


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _csv_row(values: list[object]) -> str:
    output = io.StringIO()
    csv.writer(output).writerow(values)
    return output.getvalue()


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
    parent_question_id: str | None,
    parent_row_id: int | None,
) -> list[object]:
    response_time = (rating.time_submitted - rating.time_started).total_seconds()
    return [
        rating.id,
        question.question_id,
        question.question_text,
        parent_question_id or "",
        parent_row_id if parent_row_id is not None else "",
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
    parent_question = aliased(Question)

    statement = (
        select(
            Rating,
            Question,
            Rater,
            rating_rank.c.rank,
            parent_question.question_id.label("parent_question_id"),
            parent_question.id.label("parent_row_id"),
        )
        .join(Question, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .outerjoin(parent_question, parent_question.id == Question.parent_question_id)
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

        async for rating, question, rater, rank, parent_id, parent_pk in result:
            counts = counts_toward_target(rank, experiment.num_ratings_per_question)
            writer.writerow(
                _build_export_row(rating, question, rater, counts, parent_id, parent_pk)
            )
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


async def stream_documents_export_csv_chunks(
    *,
    experiment_id: int,
    db: AsyncSession,
) -> AsyncIterator[str]:
    """Stream each parent document once.

    Fetches one parent row at a time and flushes the CSV buffer by UTF-8 byte
    size so a longbench document cannot multiply across a 1000-row chunk.
    """
    await fetch_experiment_or_404(experiment_id, db)

    logger.info(
        "Documents export started",
        extra={"attributes": {"experiment_id": experiment_id}},
    )

    yield _csv_row(list(DOCUMENT_EXPORT_COLUMNS))

    parent_ids = (
        select(Question.parent_question_id)
        .where(Question.experiment_id == experiment_id)
        .where(Question.parent_question_id.is_not(None))
        .distinct()
    )
    statement = (
        select(Question.id, Question.question_id, Question.question_text)
        .where(Question.experiment_id == experiment_id)
        .where(Question.id.in_(parent_ids))
        .order_by(Question.id)
        .execution_options(stream_results=True, yield_per=1)
    )
    result = await db.stream(statement)

    chunk_limit = _document_chunk_max_bytes()
    pending = ""
    pending_bytes = 0
    total_rows = 0
    try:
        async for row_id, question_id, question_text in result:
            row_text = _csv_row([row_id, question_id, question_text])
            row_bytes = _utf8_size(row_text)
            if pending_bytes and pending_bytes + row_bytes >= chunk_limit:
                yield pending
                pending = row_text
                pending_bytes = row_bytes
            else:
                pending += row_text
                pending_bytes += row_bytes
                if pending_bytes >= chunk_limit:
                    yield pending
                    pending = ""
                    pending_bytes = 0
            total_rows += 1

        if pending:
            yield pending

        logger.info(
            "Documents export completed",
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
