"""Shared database query helpers for core domain objects.

These are used across multiple service domains. Domain-specific queries
(e.g. rater-only or admin-only logic) stay in their respective modules.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Experiment, Question, Rater


async def fetch_experiment_or_404(experiment_id: int, db: AsyncSession) -> Experiment:
    experiment = (
        await db.execute(select(Experiment).where(Experiment.id == experiment_id))
    ).scalar_one_or_none()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return experiment


async def fetch_rater_or_404(rater_id: int, db: AsyncSession) -> Rater:
    rater = (await db.execute(select(Rater).where(Rater.id == rater_id))).scalar_one_or_none()
    if not rater:
        raise HTTPException(status_code=404, detail="Rater not found")
    return rater


async def fetch_question_or_404(question_id: int, db: AsyncSession) -> Question:
    question = (
        await db.execute(select(Question).where(Question.id == question_id))
    ).scalar_one_or_none()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    return question


def parent_question_ids_subquery():
    """Subquery yielding ids of questions referenced as a parent.

    Parent rows are header-only context for their children; they are never
    assigned to raters and never receive ratings, so they must be excluded
    from question counts, completion checks, and Prolific recommendations.
    """
    return (
        select(Question.parent_question_id)
        .where(Question.parent_question_id.is_not(None))
        .distinct()
    )


async def fetch_parent_question_text(
    parent_question_id: int,
    db: AsyncSession,
) -> str | None:
    return (
        await db.execute(select(Question.question_text).where(Question.id == parent_question_id))
    ).scalar_one_or_none()
