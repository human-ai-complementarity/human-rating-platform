"""Programmatic read-only API for CLI / inference-pipeline clients.

Authenticated with a static bearer key (``Authorization: Bearer <key>``, see
``require_api_key``) rather than the browser session cookie the dashboard uses,
so a script can fetch experiment data directly. Versioned under ``/api/v1`` to
keep this contract stable as new data surfaces are added.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_api_key
from database import get_session
from models import ExperimentStatus
from schemas import ExperimentResponse, V1RatingsPage
from services import admin as admin_service
from services import v1 as v1_service

router = APIRouter(
    prefix="/v1",
    tags=["v1"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/experiments", response_model=list[ExperimentResponse])
async def list_experiments(
    ids: list[int] | None = Query(None, description="Fetch exactly these experiment ids (batch)."),
    status: ExperimentStatus | None = Query(None),
    search: str | None = Query(None, max_length=255),
    include_archived: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_session),
):
    """List experiments for discovery, or fetch a specific batch via ``ids``.

    An ``ids`` set returns exactly those experiments regardless of archived
    state; without it, results follow the same active/archived + status/search
    filtering as the dashboard list.
    """
    return await admin_service.list_experiments(
        skip=skip,
        limit=limit,
        include_archived=include_archived,
        status=status,
        search=search,
        ids=ids,
        db=db,
    )


@router.get("/experiments/{experiment_id}", response_model=ExperimentResponse)
async def get_experiment(
    experiment_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.get_experiment(experiment_id=experiment_id, db=db)


@router.get("/experiments/{experiment_id}/ratings", response_model=V1RatingsPage)
async def list_experiment_ratings(
    experiment_id: int,
    include_preview: bool = Query(False, description="Include ratings from preview raters."),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
    db: AsyncSession = Depends(get_session),
):
    """Raw human ratings for an experiment, paginated. Page until offset >= total."""
    return await v1_service.list_experiment_ratings(
        experiment_id=experiment_id,
        db=db,
        limit=limit,
        offset=offset,
        include_preview=include_preview,
    )
