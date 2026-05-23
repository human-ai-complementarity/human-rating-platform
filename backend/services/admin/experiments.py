from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import Experiment, ExperimentRound, Question, Rating, Rater
from schemas import ExperimentCreate, ExperimentResponse, ExperimentUpdate
from .mappers import build_experiment_response
from fastapi import HTTPException
from .prolific import delete_study
from services.assistance.registry import get_method
from services.queries import parent_question_ids_subquery
from .queries import (
    fetch_experiment_or_404,
    fetch_total_questions_for_experiment,
    fetch_total_ratings_for_experiment,
)

logger = logging.getLogger(__name__)


async def create_experiment(
    payload: ExperimentCreate,
    db: AsyncSession,
) -> ExperimentResponse:
    try:
        get_method(payload.assistance_method)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    db_experiment = Experiment(
        name=payload.name,
        num_ratings_per_question=payload.num_ratings_per_question,
        prolific_completion_url=payload.prolific_completion_url,
        assistance_method=payload.assistance_method,
        assistance_params=json.dumps(payload.assistance_params)
        if payload.assistance_params
        else None,
    )
    db.add(db_experiment)
    await db.commit()
    await db.refresh(db_experiment)

    logger.info(
        "Experiment created",
        extra={
            "attributes": {
                "experiment_id": db_experiment.id,
                "experiment_name": db_experiment.name,
            }
        },
    )
    return build_experiment_response(db_experiment, question_count=0, rating_count=0)


async def list_experiments(
    skip: int,
    limit: int,
    db: AsyncSession,
) -> list[ExperimentResponse]:
    question_counts = (
        select(
            Question.experiment_id,
            func.count(Question.id).label("question_count"),
        )
        .where(Question.id.notin_(parent_question_ids_subquery()))
        .group_by(Question.experiment_id)
        .subquery()
    )

    rating_counts = (
        select(
            Question.experiment_id,
            func.count(Rating.id).label("rating_count"),
        )
        .join(Rating, Rating.question_id == Question.id)
        .group_by(Question.experiment_id)
        .subquery()
    )

    rows = (
        await db.execute(
            select(
                Experiment,
                func.coalesce(question_counts.c.question_count, 0).label("question_count"),
                func.coalesce(rating_counts.c.rating_count, 0).label("rating_count"),
            )
            .outerjoin(question_counts, Experiment.id == question_counts.c.experiment_id)
            .outerjoin(rating_counts, Experiment.id == rating_counts.c.experiment_id)
            .order_by(Experiment.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
    ).all()

    return [
        build_experiment_response(
            experiment,
            question_count=int(question_count or 0),
            rating_count=int(rating_count or 0),
        )
        for experiment, question_count, rating_count in rows
    ]


async def update_experiment(
    experiment_id: int,
    payload: ExperimentUpdate,
    db: AsyncSession,
) -> ExperimentResponse:
    try:
        get_method(payload.assistance_method)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    experiment = await fetch_experiment_or_404(experiment_id, db)
    experiment.assistance_method = payload.assistance_method
    if payload.assistance_params is not None:
        experiment.assistance_params = json.dumps(payload.assistance_params)
    await db.commit()
    await db.refresh(experiment)

    question_count = await fetch_total_questions_for_experiment(experiment_id, db)
    rating_count = await fetch_total_ratings_for_experiment(experiment_id, db)
    return build_experiment_response(
        experiment, question_count=question_count, rating_count=rating_count
    )


async def delete_experiment(
    experiment_id: int,
    db: AsyncSession,
) -> dict[str, str]:
    settings = get_settings()
    experiment = await fetch_experiment_or_404(experiment_id, db)
    experiment_name = experiment.name

    if settings.prolific.enabled:
        round_study_ids = (
            (
                await db.execute(
                    select(ExperimentRound.prolific_study_id).where(
                        ExperimentRound.experiment_id == experiment_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for study_id in round_study_ids:
            try:
                await delete_study(
                    settings=settings.prolific,
                    study_id=study_id,
                )
                logger.info(
                    "Prolific study deleted",
                    extra={"attributes": {"study_id": study_id}},
                )
            except Exception:
                logger.warning(
                    "Failed to delete Prolific study (continuing with local delete)",
                    exc_info=True,
                    extra={"attributes": {"study_id": study_id}},
                )

    await db.delete(experiment)
    await db.commit()

    logger.info(
        "Experiment deleted",
        extra={
            "attributes": {
                "experiment_id": experiment_id,
                "experiment_name": experiment_name,
            }
        },
    )
    return {"message": "Experiment deleted successfully"}


async def get_experiment_stats(
    experiment_id: int,
    db: AsyncSession,
    *,
    include_preview: bool = False,
) -> dict[str, Any]:
    experiment = await fetch_experiment_or_404(experiment_id, db)

    total_questions = await fetch_total_questions_for_experiment(experiment_id, db)

    ratings_stmt = (
        select(func.count(Rating.id))
        .join(Question, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .where(Question.experiment_id == experiment_id)
    )
    raters_stmt = select(func.count(Rater.id)).where(Rater.experiment_id == experiment_id)
    complete_stmt = (
        select(Question.id)
        .join(Rating, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .where(Question.experiment_id == experiment_id)
        .where(Question.id.notin_(parent_question_ids_subquery()))
        .group_by(Question.id)
        .having(func.count(Rating.id) >= experiment.num_ratings_per_question)
    )

    if not include_preview:
        preview_filter = Rater.is_preview == False  # noqa: E712
        ratings_stmt = ratings_stmt.where(preview_filter)
        raters_stmt = raters_stmt.where(preview_filter)
        complete_stmt = complete_stmt.where(preview_filter)

    total_ratings = (await db.execute(ratings_stmt)).scalar_one()
    total_raters = (await db.execute(raters_stmt)).scalar_one()
    questions_complete = len((await db.execute(complete_stmt)).all())

    return {
        "experiment_name": experiment.name,
        "total_questions": total_questions,
        "questions_complete": int(questions_complete),
        "total_ratings": int(total_ratings or 0),
        "total_raters": int(total_raters or 0),
        "target_ratings_per_question": experiment.num_ratings_per_question,
    }
