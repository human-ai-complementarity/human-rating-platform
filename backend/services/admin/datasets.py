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
from .groups import assert_waves_unused_except, dataset_has_groups


def normalize_waves(waves: list[str]) -> list[str]:
    """Lowercase and dedupe wave tokens, preserving first-seen order.

    Waves are enum-like tokens ("fall25", "sp26"); lowercasing both here and
    on the group's attribution wave (follow-up) makes membership checks
    casing-proof.
    """
    seen: set[str] = set()
    result: list[str] = []
    for wave in waves:
        token = wave.strip().lower()
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def _to_response(dataset: Dataset) -> DatasetResponse:
    return DatasetResponse(
        id=dataset.id,
        name=dataset.name,
        waves=json.loads(dataset.waves),
        created_at=dataset.created_at,
    )


def _conflict(name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f'Dataset "{name}" already exists (names are case-insensitive).',
    )


async def _fetch_dataset_or_404(dataset_id: int, db: AsyncSession) -> Dataset:
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


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
    await _commit_name_change(dataset, db)
    return _to_response(dataset)


async def list_datasets(db: AsyncSession) -> list[DatasetResponse]:
    result = await db.execute(select(Dataset).order_by(func.lower(Dataset.name)))
    return [_to_response(dataset) for dataset in result.scalars().all()]


async def get_dataset(dataset_id: int, db: AsyncSession) -> DatasetResponse:
    return _to_response(await _fetch_dataset_or_404(dataset_id, db))


async def update_dataset(
    dataset_id: int, payload: DatasetUpdate, db: AsyncSession
) -> DatasetResponse:
    dataset = await _fetch_dataset_or_404(dataset_id, db)

    if payload.name is not None:
        await _check_name_available(payload.name, db, exclude_id=dataset_id)
        dataset.name = payload.name
    if payload.waves is not None:
        waves = normalize_waves(payload.waves)
        await assert_waves_unused_except(dataset_id, waves, db)
        dataset.waves = json.dumps(waves)

    await _commit_name_change(dataset, db)
    return _to_response(dataset)


async def delete_dataset(dataset_id: int, db: AsyncSession) -> None:
    dataset = await _fetch_dataset_or_404(dataset_id, db)
    if await dataset_has_groups(dataset_id, db):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete a dataset that still has experiment groups.",
        )
    await db.delete(dataset)
    await db.commit()
