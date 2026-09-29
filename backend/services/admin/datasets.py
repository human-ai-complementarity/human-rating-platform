"""Dataset CRUD.

Datasets are the identity anchor for grouping experiments: a canonical row per
dataset, named after the inference-pipeline card where one exists, so identity
can't drift ("SWE-bench" vs "swebench"). Experiment groups reference a
dataset and pick an attribution wave from its `waves` set.
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Dataset
from schemas import DatasetCreate, DatasetResponse, DatasetUpdate
from services.queries import fetch_dataset_or_404
from .dataset_card import (
    CARD_FIELDS,
    card_values_from_row,
    dataset_readiness,
    write_card_values,
)
from .groups import assert_waves_unused_except, dataset_has_groups
from .waves import normalize_waves


def _to_response(dataset: Dataset) -> DatasetResponse:
    readiness = dataset_readiness(dataset)
    return DatasetResponse(
        id=dataset.id,
        name=dataset.name,
        waves=json.loads(dataset.waves),
        created_at=dataset.created_at,
        launch_ready=readiness.launch_ready,
        missing_for_launch=readiness.missing_for_launch,
        complete=readiness.complete,
        missing_for_complete=readiness.missing_for_complete,
        **card_values_from_row(dataset),
    )


def _payload_card_values(payload: DatasetCreate | DatasetUpdate) -> dict[str, object]:
    """Card fields the request actually sent.

    Keyed off `model_fields_set` rather than "is not None" so an explicit
    `null` clears a card field, while omitting it leaves the stored value
    alone — the same partial-PATCH contract the rest of this module uses.
    """
    return {
        field: getattr(payload, field) for field in CARD_FIELDS if field in payload.model_fields_set
    }


def _conflict(name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f'Dataset "{name}" already exists (names are case-insensitive).',
    )


async def _check_name_available(name: str, db: AsyncSession, exclude_id: int | None = None) -> None:
    """409 if another dataset already holds `name` case-insensitively.

    `exclude_id` exempts the dataset being renamed, so recasing your own name
    is not a conflict.
    """
    result = await db.execute(select(Dataset).where(func.lower(Dataset.name) == name.lower()))
    existing = result.scalar_one_or_none()
    if existing is not None and existing.id != exclude_id:
        raise _conflict(existing.name)


async def _commit_name_change(dataset: Dataset, db: AsyncSession) -> None:
    """Commit, converting a unique-index race on the name into the same 409
    the pre-check gives (the lower(name) index is the backstop)."""
    name = dataset.name
    db.add(dataset)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise _conflict(name) from e
    await db.refresh(dataset)


async def create_dataset(payload: DatasetCreate, db: AsyncSession) -> DatasetResponse:
    await _check_name_available(payload.name, db)
    dataset = Dataset(name=payload.name, waves=json.dumps(normalize_waves(payload.waves)))
    write_card_values(dataset, _payload_card_values(payload))
    await _commit_name_change(dataset, db)
    return _to_response(dataset)


async def list_datasets(db: AsyncSession) -> list[DatasetResponse]:
    result = await db.execute(select(Dataset).order_by(func.lower(Dataset.name)))
    return [_to_response(dataset) for dataset in result.scalars().all()]


async def get_dataset(dataset_id: int, db: AsyncSession) -> DatasetResponse:
    return _to_response(await fetch_dataset_or_404(dataset_id, db))


async def update_dataset(
    dataset_id: int, payload: DatasetUpdate, db: AsyncSession
) -> DatasetResponse:
    dataset = await fetch_dataset_or_404(dataset_id, db)

    if payload.name is not None:
        await _check_name_available(payload.name, db, exclude_id=dataset_id)
        dataset.name = payload.name
    if payload.waves is not None:
        waves = normalize_waves(payload.waves)
        await assert_waves_unused_except(dataset_id, waves, db)
        dataset.waves = json.dumps(waves)
    write_card_values(dataset, _payload_card_values(payload))

    await _commit_name_change(dataset, db)
    return _to_response(dataset)


async def delete_dataset(dataset_id: int, db: AsyncSession) -> None:
    dataset = await fetch_dataset_or_404(dataset_id, db)
    if await dataset_has_groups(dataset_id, db):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete a dataset that still has experiment groups.",
        )
    await db.delete(dataset)
    await db.commit()
