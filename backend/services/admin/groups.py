"""Experiment-group CRUD.

A group is the collection-run container: one dataset × one attribution wave.
Experiments in a group inherit both. `dataset_id` and `wave` lock once any
member experiment leaves DRAFT; the name stays editable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Dataset, Experiment, ExperimentGroup, ExperimentStatus
from schemas import ExperimentGroupCreate, ExperimentGroupResponse, ExperimentGroupUpdate


@dataclass(frozen=True)
class GroupSnapshot:
    """Denormalized group + dataset fields hung on ExperimentResponse."""

    group_id: int
    group_name: str
    dataset_id: int
    dataset_name: str
    wave: str


def _dataset_waves(dataset: Dataset) -> list[str]:
    return json.loads(dataset.waves)


def resolve_attribution_wave(
    dataset: Dataset,
    requested: str | None,
    current: str | None = None,
) -> str:
    """Pick the group's wave from the dataset's membership set.

    Explicit `requested` wins (must be in the set). Otherwise keep `current`
    if it is still a member (dataset reassignment that preserves the wave).
    Otherwise auto-fill a singleton, or 400 if the set is empty / ambiguous.
    """
    waves = _dataset_waves(dataset)
    if requested is not None:
        token = requested.strip().lower()
        if token not in waves:
            raise HTTPException(
                status_code=400,
                detail=(f'Wave "{token}" is not in dataset "{dataset.name}"\'s wave set {waves}.'),
            )
        return token
    if current is not None and current in waves:
        return current
    if len(waves) == 1:
        return waves[0]
    if not waves:
        raise HTTPException(
            status_code=400,
            detail=f'Dataset "{dataset.name}" has no waves; add waves before creating a group.',
        )
    raise HTTPException(
        status_code=400,
        detail=(
            f'Dataset "{dataset.name}" is in multiple waves {waves}; '
            "specify which one this group is for."
        ),
    )


async def fetch_dataset_or_404(dataset_id: int, db: AsyncSession) -> Dataset:
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


async def fetch_group_or_404(group_id: int, db: AsyncSession) -> ExperimentGroup:
    group = await db.get(ExperimentGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Experiment group not found")
    return group


async def group_is_locked(group_id: int, db: AsyncSession) -> bool:
    """True once any member experiment has left DRAFT."""
    result = await db.execute(
        select(Experiment.id)
        .where(
            Experiment.group_id == group_id,
            Experiment.status != ExperimentStatus.DRAFT,
        )
        .limit(1)
    )
    return result.first() is not None


async def fetch_group_snapshots(group_ids: list[int], db: AsyncSession) -> dict[int, GroupSnapshot]:
    """Batch-load group + dataset rows for experiment-response enrichment."""
    unique_ids = list({gid for gid in group_ids if gid is not None})
    if not unique_ids:
        return {}
    rows = (
        await db.execute(
            select(ExperimentGroup, Dataset)
            .join(Dataset, Dataset.id == ExperimentGroup.dataset_id)
            .where(ExperimentGroup.id.in_(unique_ids))
        )
    ).all()
    return {
        group.id: GroupSnapshot(
            group_id=group.id,
            group_name=group.name,
            dataset_id=dataset.id,
            dataset_name=dataset.name,
            wave=group.wave,
        )
        for group, dataset in rows
    }


async def fetch_group_snapshot(group_id: int | None, db: AsyncSession) -> GroupSnapshot | None:
    if group_id is None:
        return None
    snapshots = await fetch_group_snapshots([group_id], db)
    return snapshots.get(group_id)


def _to_response(
    group: ExperimentGroup, dataset_name: str, experiment_count: int
) -> ExperimentGroupResponse:
    return ExperimentGroupResponse(
        id=group.id,
        name=group.name,
        dataset_id=group.dataset_id,
        dataset_name=dataset_name,
        wave=group.wave,
        experiment_count=experiment_count,
        created_at=group.created_at,
    )


async def _experiment_counts(group_ids: list[int], db: AsyncSession) -> dict[int, int]:
    if not group_ids:
        return {}
    rows = (
        await db.execute(
            select(Experiment.group_id, func.count(Experiment.id))
            .where(Experiment.group_id.in_(group_ids))
            .group_by(Experiment.group_id)
        )
    ).all()
    return {group_id: int(count) for group_id, count in rows}


async def _check_name_available(
    name: str, dataset_id: int, db: AsyncSession, exclude_id: int | None = None
) -> None:
    result = await db.execute(
        select(ExperimentGroup).where(
            ExperimentGroup.dataset_id == dataset_id,
            func.lower(ExperimentGroup.name) == name.lower(),
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None and existing.id != exclude_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f'Group "{existing.name}" already exists on this dataset '
                "(names are case-insensitive)."
            ),
        )


async def _check_pair_available(
    dataset_id: int,
    wave: str,
    db: AsyncSession,
    exclude_id: int | None = None,
) -> None:
    result = await db.execute(
        select(ExperimentGroup).where(
            ExperimentGroup.dataset_id == dataset_id,
            ExperimentGroup.wave == wave,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None and existing.id != exclude_id:
        raise HTTPException(
            status_code=409,
            detail=f'A group for this dataset in wave "{wave}" already exists.',
        )


async def _commit_group(group: ExperimentGroup, db: AsyncSession) -> None:
    db.add(group)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Group conflicts with an existing row (name or dataset×wave).",
        ) from e
    await db.refresh(group)


async def create_group(payload: ExperimentGroupCreate, db: AsyncSession) -> ExperimentGroupResponse:
    dataset = await fetch_dataset_or_404(payload.dataset_id, db)
    wave = resolve_attribution_wave(dataset, payload.wave)
    await _check_name_available(payload.name, dataset.id, db)
    await _check_pair_available(dataset.id, wave, db)

    group = ExperimentGroup(name=payload.name, dataset_id=dataset.id, wave=wave)
    await _commit_group(group, db)
    return _to_response(group, dataset.name, experiment_count=0)


async def list_groups(
    db: AsyncSession,
    dataset_id: int | None = None,
    wave: str | None = None,
) -> list[ExperimentGroupResponse]:
    stmt = select(ExperimentGroup, Dataset).join(Dataset, Dataset.id == ExperimentGroup.dataset_id)
    if dataset_id is not None:
        stmt = stmt.where(ExperimentGroup.dataset_id == dataset_id)
    if wave is not None:
        stmt = stmt.where(ExperimentGroup.wave == wave.strip().lower())
    stmt = stmt.order_by(func.lower(ExperimentGroup.name))
    rows = (await db.execute(stmt)).all()
    counts = await _experiment_counts([group.id for group, _ in rows], db)
    return [_to_response(group, dataset.name, counts.get(group.id, 0)) for group, dataset in rows]


async def get_group(group_id: int, db: AsyncSession) -> ExperimentGroupResponse:
    group = await fetch_group_or_404(group_id, db)
    dataset = await fetch_dataset_or_404(group.dataset_id, db)
    counts = await _experiment_counts([group.id], db)
    return _to_response(group, dataset.name, counts.get(group.id, 0))


async def update_group(
    group_id: int, payload: ExperimentGroupUpdate, db: AsyncSession
) -> ExperimentGroupResponse:
    group = await fetch_group_or_404(group_id, db)
    dataset_changing = payload.dataset_id is not None and payload.dataset_id != group.dataset_id
    wave_requested = payload.wave is not None and payload.wave.strip().lower() != group.wave

    if (dataset_changing or wave_requested) and await group_is_locked(group_id, db):
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot change dataset or wave: a launched experiment in this "
                "group has locked those fields."
            ),
        )

    dataset_id = payload.dataset_id if payload.dataset_id is not None else group.dataset_id
    dataset = await fetch_dataset_or_404(dataset_id, db)
    # Keep the current wave across a dataset move when it is still a member;
    # otherwise auto-fill / require an explicit pick (see resolve_attribution_wave).
    current_wave = None if dataset_changing else group.wave
    requested = payload.wave if payload.wave is not None else None
    wave = resolve_attribution_wave(dataset, requested, current=current_wave)

    if payload.name is not None:
        await _check_name_available(payload.name, dataset_id, db, exclude_id=group_id)
        group.name = payload.name
    elif dataset_changing:
        # Name stays; still has to be free on the destination dataset.
        await _check_name_available(group.name, dataset_id, db, exclude_id=group_id)

    await _check_pair_available(dataset_id, wave, db, exclude_id=group_id)
    group.dataset_id = dataset_id
    group.wave = wave

    await _commit_group(group, db)
    counts = await _experiment_counts([group.id], db)
    return _to_response(group, dataset.name, counts.get(group.id, 0))


async def delete_group(group_id: int, db: AsyncSession) -> None:
    group = await fetch_group_or_404(group_id, db)
    counts = await _experiment_counts([group_id], db)
    if counts.get(group_id, 0):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete a group that still has experiments; reassign or delete them first.",
        )
    await db.delete(group)
    await db.commit()


async def assert_waves_unused_except(
    dataset_id: int, allowed_waves: list[str], db: AsyncSession
) -> None:
    """Reject shrinking a dataset's wave set if a group still uses a removed token."""
    result = await db.execute(
        select(ExperimentGroup.wave).where(ExperimentGroup.dataset_id == dataset_id)
    )
    in_use = set(result.scalars().all())
    missing = sorted(in_use - set(allowed_waves))
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot remove wave(s) still used by groups: {missing}.",
        )


async def dataset_has_groups(dataset_id: int, db: AsyncSession) -> bool:
    result = await db.execute(
        select(ExperimentGroup.id).where(ExperimentGroup.dataset_id == dataset_id).limit(1)
    )
    return result.first() is not None
