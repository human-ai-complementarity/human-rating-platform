"""One-time catalog seed + experiment-group backfill.

Datasets are named after inference-pipeline cards (the cross-repo join key).
This module vendors the *scheduled* cards — those with a non-empty
`inclusion_reasons` wave set — as a snapshot. Automated card sync is a
deferred follow-up; update `PIPELINE_DATASETS` when the pipeline roster
changes.

`sync_catalog` is idempotent: it creates missing dataset rows (and unions
catalog waves onto existing same-name rows), then assigns *ungrouped*
experiments whose upload filenames match a card. Wave is the dataset
singleton when there is one, otherwise a wave token found in the
experiment name / internal name / filenames. Dual-wave cards with no
signal are left ungrouped. Assignment writes `group_id` directly so
already-launched collections can be attached (the admin PATCH lock does
not apply here).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Dataset, Experiment, ExperimentGroup, Upload
from schemas import (
    CatalogAssignment,
    CatalogEntry,
    CatalogSkip,
    CatalogSyncResponse,
)
from .groups import resolve_attribution_wave
from .waves import normalize_waves

# Scheduled inference-pipeline cards (`inclusion_reasons` non-empty).
# Snapshot of pipeline/cards.py; names are stored verbatim.
PIPELINE_DATASETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gpqa_diamond", ("fall25",)),
    ("hle_rolling", ("fall25",)),
    ("QuALITY_dev", ("fall25",)),
    ("hidden_agenda", ("fall25",)),
    ("web_lies", ("fall25",)),
    ("bbeh_mini", ("fall25", "sp26")),
    ("FACTS_search_public", ("fall25", "sp26")),
    ("shade_arena", ("fall25", "sp26")),
    ("culturalbench_hard", ("sp26",)),
    ("safeagentbench", ("sp26",)),
    ("safeagentbench_abstracted", ("sp26",)),
    ("longsafety", ("sp26",)),
    ("longbenchv2", ("sp26",)),
    ("bbeh_safety", ("sp26",)),
    ("liars_bench", ("sp26",)),
    ("attunebench_pairwise", ("sp26",)),
    ("multidimensional_difference_awareness", ("sp26",)),
    ("steganographic_collusion", ("sum26",)),
    ("find_the_flaws_modified_gpqa_flaw", ("sum26",)),
    ("find_the_flaws_cels_lojban_match", ("sum26",)),
)

# Longer tokens first so "fall26" is not eaten by a future "fall2" and so
# "fall25" / "fall26" stay distinct.
_WAVE_TOKENS = ("fall26", "sum26", "fall25", "sp26")


def catalog_entries() -> list[CatalogEntry]:
    return [CatalogEntry(name=name, waves=list(waves)) for name, waves in PIPELINE_DATASETS]


def match_card_name(filename: str, card_names: list[str] | None = None) -> str | None:
    """Return the catalog card a pipeline export filename belongs to.

    Pipeline exports look like `{card}_n{count}.csv/.parquet`. Longest card
    name wins so `safeagentbench_abstracted_n10` does not attach to
    `safeagentbench`.
    """
    names = card_names if card_names is not None else [name for name, _ in PIPELINE_DATASETS]
    stem = PurePosixPath(filename.replace("\\", "/")).name.lower()
    matches = [
        name
        for name in names
        if stem == name.lower()
        or stem.startswith(f"{name.lower()}_")
        or stem.startswith(f"{name.lower()}.")
    ]
    if not matches:
        return None
    return max(matches, key=lambda name: len(name))


def infer_wave(blobs: list[str], dataset_waves: list[str]) -> str | None:
    """Pick an attribution wave from a dataset's membership set.

    A singleton set is unambiguous. Otherwise look for membership tokens in
    the supplied text (name, internal name, filenames). Zero or several
    hits → None (leave ungrouped).
    """
    if len(dataset_waves) == 1:
        return dataset_waves[0]
    allowed = set(dataset_waves)
    haystack = " ".join(blobs).lower()
    found = [token for token in _WAVE_TOKENS if token in allowed and token in haystack]
    if len(found) == 1:
        return found[0]
    return None


@dataclass
class _DatasetRow:
    id: int
    name: str
    waves: list[str]


async def sync_catalog(db: AsyncSession) -> CatalogSyncResponse:
    created, updated, by_lower = await _seed_datasets(db)
    groups_created, assigned, skipped = await _assign_experiments(db, by_lower)
    await db.commit()
    return CatalogSyncResponse(
        datasets_created=created,
        datasets_updated=updated,
        groups_created=groups_created,
        experiments_assigned=assigned,
        experiments_skipped=skipped,
    )


async def _seed_datasets(
    db: AsyncSession,
) -> tuple[list[str], list[str], dict[str, _DatasetRow]]:
    existing = (await db.execute(select(Dataset))).scalars().all()
    by_lower = {
        dataset.name.lower(): _DatasetRow(
            id=dataset.id, name=dataset.name, waves=json.loads(dataset.waves)
        )
        for dataset in existing
    }
    created: list[str] = []
    updated: list[str] = []

    for name, waves in PIPELINE_DATASETS:
        catalog_waves = normalize_waves(list(waves))
        row = by_lower.get(name.lower())
        if row is None:
            dataset, created_now = await _insert_or_get_dataset(name, catalog_waves, db)
            row = _DatasetRow(
                id=dataset.id,
                name=dataset.name,
                waves=json.loads(dataset.waves),
            )
            by_lower[name.lower()] = row
            if created_now:
                created.append(name)
                continue
        merged = normalize_waves([*row.waves, *catalog_waves])
        if merged != row.waves:
            dataset = await db.get(Dataset, row.id)
            assert dataset is not None
            dataset.waves = json.dumps(merged)
            row.waves = merged
            updated.append(row.name)

    return created, updated, by_lower


async def _insert_or_get_dataset(
    name: str, waves: list[str], db: AsyncSession
) -> tuple[Dataset, bool]:
    """Insert a catalog dataset, or return the row that won a concurrent insert.

    Two syncs can both miss the SELECT and both INSERT; `uq_datasets_name_lower`
    rejects the loser. Re-read rather than 409 — sync is meant to be
    idempotent. A savepoint keeps datasets already inserted in this pass.
    """
    try:
        async with db.begin_nested():
            dataset = Dataset(name=name, waves=json.dumps(waves))
            db.add(dataset)
            await db.flush()
            return dataset, True
    except IntegrityError:
        existing = (
            await db.execute(select(Dataset).where(func.lower(Dataset.name) == name.lower()))
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False


async def _assign_experiments(
    db: AsyncSession, by_lower: dict[str, _DatasetRow]
) -> tuple[list[str], list[CatalogAssignment], list[CatalogSkip]]:
    experiments = (
        (await db.execute(select(Experiment).where(Experiment.group_id.is_(None)))).scalars().all()
    )
    experiment_ids = [experiment.id for experiment in experiments]
    filenames_by_experiment: dict[int, list[str]] = {eid: [] for eid in experiment_ids}
    if experiment_ids:
        uploads = (
            (await db.execute(select(Upload).where(Upload.experiment_id.in_(experiment_ids))))
            .scalars()
            .all()
        )
        for upload in uploads:
            filenames_by_experiment[upload.experiment_id].append(upload.filename)

    groups_created: list[str] = []
    assigned: list[CatalogAssignment] = []
    skipped: list[CatalogSkip] = []
    card_names = [name for name, _ in PIPELINE_DATASETS]

    for experiment in experiments:
        filenames = filenames_by_experiment.get(experiment.id, [])
        cards = {match_card_name(filename, card_names) for filename in filenames}
        cards.discard(None)
        if len(cards) == 0:
            if filenames:
                skipped.append(
                    CatalogSkip(
                        experiment_id=experiment.id,
                        experiment_name=experiment.name,
                        reason="no_upload_match",
                    )
                )
            continue
        if len(cards) > 1:
            skipped.append(
                CatalogSkip(
                    experiment_id=experiment.id,
                    experiment_name=experiment.name,
                    reason="ambiguous_dataset",
                )
            )
            continue

        card = next(iter(cards))
        assert card is not None
        dataset = by_lower[card.lower()]
        wave = infer_wave(
            [experiment.name, experiment.internal_name or "", *filenames],
            dataset.waves,
        )
        if wave is None:
            skipped.append(
                CatalogSkip(
                    experiment_id=experiment.id,
                    experiment_name=experiment.name,
                    reason="ambiguous_wave",
                )
            )
            continue

        group, created = await _get_or_create_group(db, dataset, wave)
        if created:
            groups_created.append(group.name)
        experiment.group_id = group.id
        assigned.append(
            CatalogAssignment(
                experiment_id=experiment.id,
                experiment_name=experiment.name,
                dataset_name=dataset.name,
                wave=wave,
                group_id=group.id,
            )
        )

    return groups_created, assigned, skipped


async def _get_or_create_group(
    db: AsyncSession, dataset: _DatasetRow, wave: str
) -> tuple[ExperimentGroup, bool]:
    existing = (
        await db.execute(
            select(ExperimentGroup).where(
                ExperimentGroup.dataset_id == dataset.id,
                ExperimentGroup.wave == wave,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    # resolve_attribution_wave keeps us honest if a human-edited wave set
    # no longer contains the token we inferred.
    dataset_row = await db.get(Dataset, dataset.id)
    assert dataset_row is not None
    wave = resolve_attribution_wave(dataset_row, wave)

    name = f"{dataset.name} {wave}"
    taken = (
        await db.execute(
            select(ExperimentGroup.id).where(
                ExperimentGroup.dataset_id == dataset.id,
                func.lower(ExperimentGroup.name) == name.lower(),
            )
        )
    ).scalar_one_or_none()
    if taken is not None:
        name = f"{dataset.name} ({wave})"

    group = ExperimentGroup(name=name, dataset_id=dataset.id, wave=wave)
    return await _insert_or_get_group(db, group)


async def _insert_or_get_group(
    db: AsyncSession, group: ExperimentGroup
) -> tuple[ExperimentGroup, bool]:
    """Insert the group, or return the row that won a concurrent dataset×wave insert."""
    try:
        async with db.begin_nested():
            db.add(group)
            await db.flush()
            return group, True
    except IntegrityError:
        existing = (
            await db.execute(
                select(ExperimentGroup).where(
                    ExperimentGroup.dataset_id == group.dataset_id,
                    ExperimentGroup.wave == group.wave,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False
