from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ConsentRecord
from services.terms import statement_ref
from .mappers import build_analytics_payload, build_empty_analytics_payload
from .queries import (
    fetch_experiment_or_404,
    fetch_ratings_for_experiment,
    fetch_timed_out_rater_count,
    fetch_total_questions_for_experiment,
)


async def fetch_consents_by_rater(
    experiment_id: int, db: AsyncSession
) -> dict[int, tuple[str, datetime]]:
    """rater_id -> ("standard v1", accepted_at) for every consent in the experiment."""
    rows = (
        await db.execute(
            select(
                ConsentRecord.rater_id,
                ConsentRecord.bundle,
                ConsentRecord.version,
                ConsentRecord.accepted_at,
            ).where(ConsentRecord.experiment_id == experiment_id)
        )
    ).all()
    return {
        rater_id: (statement_ref(bundle, version), accepted_at)
        for rater_id, bundle, version, accepted_at in rows
    }


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
        consents=await fetch_consents_by_rater(experiment_id, db),
    )
