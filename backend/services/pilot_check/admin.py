"""The admin API's view of pilot check runs."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from database import Database
from models import Experiment
from schemas import PilotCheckRunResponse
from services.admin.queries import fetch_total_questions_for_experiment
from services.queries import fetch_experiment_or_404

from .config import get_pilot_check_config
from .runs import (
    build_run_response,
    compute_fingerprint,
    fetch_latest_upload_ids,
    get_run,
    latest_run,
    start_run,
)


async def _current_fingerprint(experiment: Experiment, db: AsyncSession) -> str:
    config = get_pilot_check_config()
    question_count = await fetch_total_questions_for_experiment(experiment.id, db)
    uploads = await fetch_latest_upload_ids([experiment.id], db)
    return compute_fingerprint(
        experiment,
        question_count=question_count,
        latest_upload_id=uploads.get(experiment.id),
        config_sha256=config.sha256,
    )


async def run_pilot_check(
    experiment_id: int, *, requested_by: str | None, db: AsyncSession, database: Database
) -> PilotCheckRunResponse:
    experiment = await fetch_experiment_or_404(experiment_id, db)
    question_count = await fetch_total_questions_for_experiment(experiment_id, db)
    run = await start_run(
        experiment,
        requested_by=requested_by,
        question_count=question_count,
        db=db,
        database=database,
    )
    return build_run_response(
        run, current_fingerprint=run.fingerprint, config=get_pilot_check_config()
    )


async def get_latest_pilot_check(experiment_id: int, db: AsyncSession) -> PilotCheckRunResponse:
    experiment = await fetch_experiment_or_404(experiment_id, db)
    run = await latest_run(experiment_id, db)
    if run is None:
        raise HTTPException(status_code=404, detail="This experiment has no pilot check run yet.")
    return build_run_response(
        run,
        current_fingerprint=await _current_fingerprint(experiment, db),
        config=get_pilot_check_config(),
    )


async def get_pilot_check_run(run_id: int, db: AsyncSession) -> PilotCheckRunResponse:
    run = await get_run(run_id, db)
    experiment = await fetch_experiment_or_404(run.experiment_id, db)
    return build_run_response(
        run,
        current_fingerprint=await _current_fingerprint(experiment, db),
        config=get_pilot_check_config(),
    )
