"""Dataset catalog: seed dataset rows from the pipeline roster and backfill groups.

Run through `backend/scripts/sync_dataset_catalog.py` — a dry run by default,
`--apply` to write. It is an operator one-off rather than an API route, so it
does not linger as an endpoint anyone with admin access can re-trigger.

Datasets are named after inference-pipeline cards (the cross-repo join key).
This module vendors the *scheduled* cards — those with a non-empty
`inclusion_reasons` wave set — as a snapshot. Automated card sync is a
deferred follow-up; update `PIPELINE_DATASETS` when the pipeline roster
changes.

`sync_dataset_catalog` is idempotent: it creates missing dataset rows (and
unions catalog waves onto existing same-name rows), then assigns *ungrouped*
experiments whose upload filenames match a card. Wave is the dataset
singleton when there is one, otherwise a wave token found in the
experiment name / internal name / filenames. Dual-wave cards with no
signal are left ungrouped. Assignment writes `group_id` directly so
already-launched collections can be attached (the admin PATCH lock does
not apply here) — which also means a wrong match on a launched experiment
cannot be undone through the API. Hence the strict filename rule in
`match_card_name`, and reading the dry run before applying.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Dataset, Experiment, ExperimentGroup, Upload
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
    ("bbeh_mini", ("fall25",)),
    ("FACTS_search_public", ("fall25", "sp26")),
    ("shade_arena", ("fall25", "sp26")),
    ("culturalbench_hard", ("sp26",)),
    ("safeagentbench", ("sp26",)),
    ("bbeh_safety", ("sp26",)),
    ("liars_bench", ("sp26",)),
    ("attunebench_pairwise", ("sp26",)),
    ("multidimensional_difference_awareness", ("sp26",)),
    ("steganographic_collusion", ("sum26",)),
    ("find_the_flaws_modified_gpqa_flaw", ("sum26",)),
    ("find_the_flaws_cels_lojban_match", ("sum26",)),
    ("gpqa_metadata_blind_answer", ("sum26",)),
    ("longbenchv2", ("sum26",)),
    ("longsafety", ("sum26",)),
    ("primevul", ("sum26",)),
)

# Wave tokens recognised in experiment names and upload filenames. Order is
# irrelevant: infer_wave collects every token present and requires exactly one.
_WAVE_TOKENS = ("fall25", "sp26", "sum26", "fall26")


@dataclass
class DatasetCatalogAssignment:
    experiment_id: int
    experiment_name: str
    dataset_name: str
    wave: str
    group_id: int
    group_name: str


@dataclass
class DatasetCatalogSkip:
    experiment_id: int
    experiment_name: str
    reason: str


@dataclass
class DatasetCatalogSyncReport:
    applied: bool
    datasets_created: list[str] = field(default_factory=list)
    datasets_updated: list[str] = field(default_factory=list)
    groups_created: list[str] = field(default_factory=list)
    experiments_assigned: list[DatasetCatalogAssignment] = field(default_factory=list)
    experiments_skipped: list[DatasetCatalogSkip] = field(default_factory=list)


_WAVE_INFIX = "|".join(re.escape(token) for token in _WAVE_TOKENS)


@lru_cache(maxsize=None)
def _export_pattern(card: str) -> re.Pattern[str]:
    card = re.escape(card.lower())
    return re.compile(
        # the bare card name, optionally with an extension
        rf"{card}(?:\..+)?"
        # {card}_n{count} or {card}_{wave}_n{count}, then anything after the count
        rf"|{card}(?:_(?:{_WAVE_INFIX}))?_n\d+(?:[._].*)?"
    )


def match_card_name(filename: str, card_names: list[str] | None = None) -> str | None:
    """Return the card a pipeline export filename belongs to, or None.

    Pipeline exports look like `{card}_n{count}`, optionally
    `{card}_{wave}_n{count}`, with any extension or suffix after the count.
    Only that shape matches. A looser `{card}_*` prefix rule attached
    `safeagentbench_abstracted_n10` to `safeagentbench` once the abstracted
    card left the roster, and would do the same to `primevul_cwe_*`. A file
    that does not fit is reported as `no_upload_match` instead, which is
    recoverable where a wrong assignment on a launched experiment is not.
    Longest card name wins if two still match.
    """
    names = card_names if card_names is not None else [name for name, _ in PIPELINE_DATASETS]
    stem = PurePosixPath(filename.replace("\\", "/")).name.lower()
    matches = [name for name in names if _export_pattern(name).fullmatch(stem)]
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


async def sync_dataset_catalog(db: AsyncSession, *, apply: bool) -> DatasetCatalogSyncReport:
    """Seed datasets and backfill groups, committing only when `apply` is true.

    The pass is one transaction that commits once at the end (inner writes use
    savepoints), so a dry run is the identical pass rolled back: its report is
    exactly what `apply` would write. `apply` has no default so every caller
    states which one it means. In a dry-run report, ids of newly created groups
    belong to rolled-back rows; identify those groups by name.
    """
    created, updated, by_lower = await _seed_datasets(db)
    groups_created, assigned, skipped = await _assign_experiments(db, by_lower)
    if apply:
        await db.commit()
    else:
        await db.rollback()
    return DatasetCatalogSyncReport(
        applied=apply,
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
) -> tuple[list[str], list[DatasetCatalogAssignment], list[DatasetCatalogSkip]]:
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
    assigned: list[DatasetCatalogAssignment] = []
    skipped: list[DatasetCatalogSkip] = []
    card_names = [name for name, _ in PIPELINE_DATASETS]

    for experiment in experiments:
        filenames = filenames_by_experiment.get(experiment.id, [])
        cards = {match_card_name(filename, card_names) for filename in filenames}
        cards.discard(None)
        if len(cards) == 0:
            if filenames:
                skipped.append(
                    DatasetCatalogSkip(
                        experiment_id=experiment.id,
                        experiment_name=experiment.name,
                        reason="no_upload_match",
                    )
                )
            continue
        if len(cards) > 1:
            skipped.append(
                DatasetCatalogSkip(
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
                DatasetCatalogSkip(
                    experiment_id=experiment.id,
                    experiment_name=experiment.name,
                    reason="ambiguous_wave",
                )
            )
            continue

        placed = await _get_or_create_group(db, dataset, wave)
        if placed is None:
            skipped.append(
                DatasetCatalogSkip(
                    experiment_id=experiment.id,
                    experiment_name=experiment.name,
                    reason="group_name_conflict",
                )
            )
            continue
        group, created = placed
        if created:
            groups_created.append(group.name)
        experiment.group_id = group.id
        assigned.append(
            DatasetCatalogAssignment(
                experiment_id=experiment.id,
                experiment_name=experiment.name,
                dataset_name=dataset.name,
                wave=wave,
                group_id=group.id,
                group_name=group.name,
            )
        )

    return groups_created, assigned, skipped


async def _get_or_create_group(
    db: AsyncSession, dataset: _DatasetRow, wave: str
) -> tuple[ExperimentGroup, bool] | None:
    """Find or create this dataset x wave group. None when it cannot be named.

    Group names are admin-editable, so both names we would generate can already
    be taken by human-renamed groups on other waves. That is a name-index
    violation, not the dataset x wave race, and the caller skips the experiment
    rather than failing a sync documented as idempotent.
    """
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

    name = await _free_group_name(db, dataset, wave)
    if name is None:
        return None

    group = ExperimentGroup(name=name, dataset_id=dataset.id, wave=wave)
    return await _insert_or_get_group(db, group)


async def _free_group_name(db: AsyncSession, dataset: _DatasetRow, wave: str) -> str | None:
    """First candidate name not already used by a group on this dataset.

    Both candidates are checked in one query. Checking only the first left the
    fallback free to collide with `uq_experiment_groups_dataset_name_lower`.
    """
    candidates = [f"{dataset.name} {wave}", f"{dataset.name} ({wave})"]
    taken = {
        name.lower()
        for name in (
            await db.execute(
                select(ExperimentGroup.name).where(
                    ExperimentGroup.dataset_id == dataset.id,
                    func.lower(ExperimentGroup.name).in_([c.lower() for c in candidates]),
                )
            )
        )
        .scalars()
        .all()
    }
    return next((c for c in candidates if c.lower() not in taken), None)


async def _insert_or_get_group(
    db: AsyncSession, group: ExperimentGroup
) -> tuple[ExperimentGroup, bool] | None:
    """Insert the group, or recover from whichever unique constraint fired.

    Two can fire: (dataset_id, wave) when a concurrent sync created the same
    group — re-read and share it — and (dataset_id, lower(name)) when someone
    took the name between our check and the insert, which is not ours to
    resolve, so the caller skips. Anything else is unexpected and re-raised
    rather than swallowed.
    """
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
        if existing is not None:
            return existing, False
        name_taken = (
            await db.execute(
                select(ExperimentGroup.id).where(
                    ExperimentGroup.dataset_id == group.dataset_id,
                    func.lower(ExperimentGroup.name) == group.name.lower(),
                )
            )
        ).scalar_one_or_none()
        if name_taken is not None:
            return None
        raise
