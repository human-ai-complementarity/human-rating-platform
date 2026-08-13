"""Dataset CRUD (design: docs/datasets-groups-design.md).

Datasets are the identity anchor for grouping experiments: a canonical row per
dataset, named after the inference-pipeline card where one exists, so identity
can't drift ("SWE-bench" vs "swebench"). Experiment groups (follow-up
migration) reference a dataset and pick an attribution wave from its `waves`
set; nothing else hangs off datasets yet.
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Dataset
from schemas import DatasetCreate, DatasetResponse, DatasetUpdate


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


async def _fetch_dataset_or_404(dataset_id: int, db: AsyncSession) -> Dataset:
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


async def _find_by_name_ci(name: str, db: AsyncSession) -> Dataset | None:
    result = await db.execute(select(Dataset).where(func.lower(Dataset.name) == name.lower()))
    return result.scalar_one_or_none()


async def create_dataset(payload: DatasetCreate, db: AsyncSession) -> DatasetResponse:
    existing = await _find_by_name_ci(payload.name, db)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f'Dataset "{existing.name}" already exists (names are case-insensitive).',
        )

    dataset = Dataset(name=payload.name, waves=json.dumps(normalize_waves(payload.waves)))
    db.add(dataset)
    try:
        await db.commit()
    except IntegrityError as e:
        # Race with a concurrent create; the lower(name) unique index caught it.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f'Dataset "{payload.name}" already exists (names are case-insensitive).',
        ) from e
    await db.refresh(dataset)
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

    if payload.name is not None and payload.name.lower() != dataset.name.lower():
        existing = await _find_by_name_ci(payload.name, db)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f'Dataset "{existing.name}" already exists (names are case-insensitive).',
            )
    if payload.name is not None:
        dataset.name = payload.name
    if payload.waves is not None:
        dataset.waves = json.dumps(normalize_waves(payload.waves))

    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    return _to_response(dataset)


async def delete_dataset(dataset_id: int, db: AsyncSession) -> None:
    # Nothing references datasets yet; once experiment groups land, deletion
    # is blocked (RESTRICT FK) while any group points here.
    dataset = await _fetch_dataset_or_404(dataset_id, db)
    await db.delete(dataset)
    await db.commit()
