"""Which questions a run looks at."""

from __future__ import annotations

import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Question
from services.queries import parent_question_ids_subquery


def choose_sample(rows: list[tuple[int, str, bool]], size: int, seed: int | None) -> list[int]:
    """Pick `size` ids from (id, question_type, is_sub_question) rows.

    Uniform without replacement, except that every question type and (when
    the upload has any) at least one sub-question is represented, so a
    dataset with one odd row type is not missed by chance.
    """
    if not rows:
        return []
    rng = random.Random(seed)
    shuffled = list(rows)
    rng.shuffle(shuffled)
    chosen: list[int] = []
    seen_types: set[str] = set()
    have_sub = False
    # One representative per stratum first, in shuffled order.
    for qid, qtype, is_sub in shuffled:
        if len(chosen) >= size:
            break
        new_type = qtype not in seen_types
        new_sub = is_sub and not have_sub
        if new_type or new_sub:
            chosen.append(qid)
            seen_types.add(qtype)
            have_sub = have_sub or is_sub
    picked = set(chosen)
    for qid, _, _ in shuffled:
        if len(chosen) >= size:
            break
        if qid not in picked:
            chosen.append(qid)
            picked.add(qid)
    # Report in id order so the sample reads like the dataset.
    return sorted(chosen)


async def sample_questions(
    experiment_id: int, *, size: int, seed: int | None, db: AsyncSession
) -> list[Question]:
    rows = (
        await db.execute(
            select(Question.id, Question.question_type, Question.parent_question_id)
            .where(Question.experiment_id == experiment_id)
            .where(Question.id.notin_(parent_question_ids_subquery()))
            .order_by(Question.id)
        )
    ).all()
    ids = choose_sample(
        [(row.id, row.question_type, row.parent_question_id is not None) for row in rows],
        size,
        seed,
    )
    if not ids:
        return []
    questions = (
        (await db.execute(select(Question).where(Question.id.in_(ids)).order_by(Question.id)))
        .scalars()
        .all()
    )
    return list(questions)
