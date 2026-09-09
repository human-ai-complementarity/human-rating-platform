from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import Question


def _question_payload_size(question: Question) -> int:
    """Approximate bytes this row contributes to an INSERT's parameter payload.

    Only the free-form text columns are worth counting — the fixed-width ones
    are noise next to a 500KB `question_text`.
    """
    return sum(
        len(value.encode("utf-8"))
        for value in (
            question.question_id,
            question.question_text,
            question.gt_answer,
            question.options,
            question.extra_data,
        )
        if value
    )


def _batch_by_payload_size(
    questions: list[Question],
    max_bytes: int,
) -> Iterator[list[Question]]:
    """Split questions into batches whose text payload stays under `max_bytes`.

    A single row larger than the cap is yielded on its own rather than dropped —
    one oversized statement is still far better than an unbounded one.
    """
    batch: list[Question] = []
    batch_bytes = 0

    for question in questions:
        size = _question_payload_size(question)
        if batch and batch_bytes + size > max_bytes:
            yield batch
            batch, batch_bytes = [], 0
        batch.append(question)
        batch_bytes += size

    if batch:
        yield batch


async def insert_questions_in_batches(
    new_questions: list[Question],
    db: AsyncSession,
) -> int:
    """Add and flush questions in payload-bounded batches. Returns batch count.

    Flushing per batch is what bounds memory: it forces each batch out as its
    own INSERT instead of letting one flush emit every row at once. The cap is
    read per call so it can be retuned by env var without a code change.

    Every caller inserting a whole dataset's worth of rows must go through this.
    A single unbounded flush over long-context rows OOM-kills a small Postgres,
    which takes the entire cluster down with it, not just the request. That is
    not left to convention: `database.register_insert_payload_guard` fails any
    INSERT into `questions` that arrives unbatched, whatever the caller.
    """
    max_bytes = get_settings().uploads.max_insert_payload_bytes

    batch_count = 0
    for batch in _batch_by_payload_size(new_questions, max_bytes):
        db.add_all(batch)
        await db.flush()
        batch_count += 1
    return batch_count
