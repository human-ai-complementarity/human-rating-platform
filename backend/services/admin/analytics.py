from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .mappers import build_analytics_payload, build_empty_analytics_payload
from .queries import (
    fetch_experiment_or_404,
    fetch_ratings_for_experiment,
    fetch_timed_out_rater_count,
    fetch_total_questions_for_experiment,
)


async def get_experiment_analytics(
    experiment_id: int,
    db: AsyncSession,
    *,
    include_preview: bool = False,
) -> dict[str, Any]:
    experiment = await fetch_experiment_or_404(experiment_id, db)
    ratings = await fetch_ratings_for_experiment(experiment_id, db, include_preview=include_preview)
    total_questions = await fetch_total_questions_for_experiment(experiment_id, db)
    timed_out_raters = await fetch_timed_out_rater_count(
        experiment_id, db, include_preview=include_preview
    )

    if not ratings:
        return build_empty_analytics_payload(
            experiment_name=experiment.name,
            total_questions=total_questions,
            timed_out_raters=timed_out_raters,
        )

    return build_analytics_payload(
        experiment_name=experiment.name,
        total_questions=total_questions,
        ratings=ratings,
        timed_out_raters=timed_out_raters,
    )
