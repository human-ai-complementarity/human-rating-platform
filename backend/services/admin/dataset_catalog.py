"""Dataset catalog: one-time backfill of past collections into datasets and groups.

Run through `backend/scripts/sync_dataset_catalog.py` — a dry run by default,
`--apply` to write. It is an operator one-off rather than an API route, so it
does not linger as an endpoint anyone with admin access can re-trigger.

Two vendored snapshots, both as of inference-pipeline commit `PIPELINE_COMMIT`
and frozen there (this is a backfill, not a card sync):

- `PIPELINE_DATASETS` — cards and the waves they are *scheduled* for (card
  `inclusion`, which the pipeline calls roadmap intent). Seeds one dataset row
  per card, named after the card: the cross-repo join key.
- `COLLECTIONS` — which experiments on this platform were collected for which
  wave, from the pipeline's own collection record. This is the only source of
  an experiment's wave. Card inclusion never is: a card's schedule changes
  after the fact (longbenchv2 and longsafety moved sp26 -> sum26 after their
  sp26 collection ran), and the pipeline treats the two as legitimately
  diverging (inference-pipeline #168).

So a group's wave is the wave its collection run was conducted for — never a
later wave that reuses the ratings; that reuse is recorded in the pipeline's
per-wave baseline folders, not by regrouping here. And a dataset's `waves`
ends up as the waves it is scheduled for *or* was collected in: an
assignment adds its wave to the dataset when the card no longer lists it.

Only experiments listed in `COLLECTIONS` are ever assigned, and each is
cross-checked first. Its upload filenames must be exports of the listed card,
its assistance method must be the listed arm, and any wave token in its names
must agree with the listed wave. One that disagrees, is archived, or is
already grouped is reported and left alone, as is every unlisted ungrouped
experiment. Assignment writes `group_id` directly, bypassing the post-launch
lock so launched collections can be attached — which also means a wrong
assignment on a launched experiment cannot be undone through the API. Read
the dry run before applying.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Dataset, Experiment, ExperimentGroup, Upload
from .groups import resolve_attribution_wave
from .waves import normalize_waves

# The inference-pipeline commit both snapshots below were taken from.
PIPELINE_COMMIT = "9f75a12"

# Cards scheduled for at least one wave (`dataset_names_for_wave`), with those
# waves. Names are stored verbatim.
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
    ("find_the_flaws_modified_gpqa_flaw", ("sum26",)),
    ("find_the_flaws_cels_lojban_match", ("sum26",)),
    ("gpqa_metadata_blind_answer", ("sum26",)),
    ("longbenchv2", ("sum26",)),
    ("longsafety", ("sum26",)),
    ("primevul", ("sum26",)),
)


@dataclass(frozen=True)
class Collection:
    """One experiment's place in the pipeline's collection record."""

    card: str
    arm: str  # the experiment's assistance_method
    wave: str


# sp26 — the SPAR spring 2026 platform pull. The ids are `IDS` in the
# pipeline's scripts/sp26_pull_platform_ratings.sh ("the id list below IS the
# wave"); each id's arm and dataset come from `EXPERIMENTS` in
# scripts/sp26_convert_platform_ratings.py, whose group names map to the cards
# noted per block.
# sum26 — the MARS summer 2026 FindTheFlaws baselines. No pipeline pull covers
# them yet; their internal names carry the SUM26 token, which the wave check
# confirms.
COLLECTIONS: dict[int, Collection] = {
    # platform_facts_search
    65: Collection("FACTS_search_public", "none", "sp26"),
    84: Collection("FACTS_search_public", "human_as_a_tool", "sp26"),
    124: Collection("FACTS_search_public", "top_n", "sp26"),
    # platform_safeagentbench
    67: Collection("safeagentbench", "none", "sp26"),
    117: Collection("safeagentbench", "human_as_a_tool", "sp26"),
    127: Collection("safeagentbench", "top_n", "sp26"),
    # platform_safeagentbench_abstract. Collected in sp26, then descheduled
    # (inference-pipeline #89), so it is not in PIPELINE_DATASETS.
    68: Collection("safeagentbench_abstracted", "none", "sp26"),
    80: Collection("safeagentbench_abstracted", "human_as_a_tool", "sp26"),
    128: Collection("safeagentbench_abstracted", "top_n", "sp26"),
    # platform_shade_arena
    71: Collection("shade_arena", "none", "sp26"),
    85: Collection("shade_arena", "human_as_a_tool", "sp26"),
    126: Collection("shade_arena", "top_n", "sp26"),
    # platform_difference_awareness
    72: Collection("multidimensional_difference_awareness", "none", "sp26"),
    120: Collection("multidimensional_difference_awareness", "human_as_a_tool", "sp26"),
    131: Collection("multidimensional_difference_awareness", "top_n", "sp26"),
    # platform_deception
    75: Collection("liars_bench", "none", "sp26"),
    122: Collection("liars_bench", "human_as_a_tool", "sp26"),
    125: Collection("liars_bench", "top_n", "sp26"),
    # platform_attunebench_pairwise
    83: Collection("attunebench_pairwise", "none", "sp26"),
    121: Collection("attunebench_pairwise", "human_as_a_tool", "sp26"),
    130: Collection("attunebench_pairwise", "top_n", "sp26"),
    # platform_culturalbench
    82: Collection("culturalbench_hard", "none", "sp26"),
    123: Collection("culturalbench_hard", "human_as_a_tool", "sp26"),
    129: Collection("culturalbench_hard", "top_n", "sp26"),
    # platform_bbeh_safety
    135: Collection("bbeh_safety", "none", "sp26"),
    136: Collection("bbeh_safety", "human_as_a_tool", "sp26"),
    236: Collection("bbeh_safety", "top_n", "sp26"),
    # platform_longbenchv2 / platform_longsafety — unassisted arms only. Both
    # cards moved to sum26 afterwards (inference-pipeline #113); the collection
    # stays sp26.
    133: Collection("longbenchv2", "none", "sp26"),
    134: Collection("longsafety", "none", "sp26"),
    # MARS sum26
    76: Collection("find_the_flaws_cels_lojban_match", "none", "sum26"),
    78: Collection("find_the_flaws_modified_gpqa_flaw", "none", "sum26"),
}

# Wave tokens recognised in experiment names and upload filenames.
_WAVE_TOKENS = ("fall25", "sp26", "sum26", "fall26")


@dataclass
class DatasetCatalogAssignment:
    experiment_id: int
    experiment_name: str
    internal_name: str | None
    dataset_name: str
    arm: str
    wave: str
    group_id: int
    group_name: str


@dataclass
class DatasetCatalogSkip:
    experiment_id: int
    experiment_name: str
    internal_name: str | None
    reason: str
    detail: str = ""


@dataclass
class DatasetCatalogSyncReport:
    applied: bool
    pipeline_commit: str
    datasets_created: list[str] = field(default_factory=list)
    datasets_updated: list[str] = field(default_factory=list)
    groups_created: list[str] = field(default_factory=list)
    experiments_assigned: list[DatasetCatalogAssignment] = field(default_factory=list)
    experiments_skipped: list[DatasetCatalogSkip] = field(default_factory=list)
    # "{dataset} {wave}" for each wave a dataset gains because a collection ran
    # in it although the card does not schedule it (e.g. "longbenchv2 sp26").
    collected_outside_schedule: list[str] = field(default_factory=list)
    # Listed experiment ids with no row in this database — expected anywhere
    # but the production database the record describes.
    manifest_missing: list[int] = field(default_factory=list)


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


def match_card_name(filename: str, card_names: Iterable[str]) -> str | None:
    """Return the card a pipeline export filename belongs to, or None.

    Pipeline exports look like `{card}_n{count}`, optionally
    `{card}_{wave}_n{count}`, with any extension or suffix after the count.
    Only that shape matches. A looser `{card}_*` prefix rule attached
    `safeagentbench_abstracted_n10` to `safeagentbench`, and would do the same
    to `primevul_cwe_*`. Longest card name wins if two still match.
    """
    stem = PurePosixPath(filename.replace("\\", "/")).name.lower()
    matches = [name for name in card_names if _export_pattern(name).fullmatch(stem)]
    if not matches:
        return None
    return max(matches, key=lambda name: len(name))


def wave_tokens(texts: Iterable[str]) -> set[str]:
    """Every wave token that appears in the given names or filenames."""
    haystack = " ".join(texts).lower()
    return {token for token in _WAVE_TOKENS if token in haystack}


@dataclass
class _DatasetRow:
    id: int
    name: str
    waves: list[str]


async def sync_dataset_catalog(
    db: AsyncSession,
    *,
    apply: bool,
    collections: Mapping[int, Collection] = COLLECTIONS,
) -> DatasetCatalogSyncReport:
    """Seed datasets and backfill groups, committing only when `apply` is true.

    The pass is one transaction that commits once at the end (inner writes use
    savepoints), so a dry run is the identical pass rolled back: its report is
    exactly what `apply` would write. `apply` has no default so every caller
    states which one it means. In a dry-run report, ids of newly created groups
    belong to rolled-back rows; identify those groups by name.
    """
    report = DatasetCatalogSyncReport(applied=apply, pipeline_commit=PIPELINE_COMMIT)
    by_lower = await _seed_datasets(db, report)
    await _assign_experiments(db, by_lower, collections, report)
    if apply:
        await db.commit()
    else:
        await db.rollback()
    return report


async def _seed_datasets(
    db: AsyncSession, report: DatasetCatalogSyncReport
) -> dict[str, _DatasetRow]:
    existing = (await db.execute(select(Dataset))).scalars().all()
    by_lower = {
        dataset.name.lower(): _DatasetRow(
            id=dataset.id, name=dataset.name, waves=json.loads(dataset.waves)
        )
        for dataset in existing
    }

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
                report.datasets_created.append(name)
                continue
        await _union_waves(db, row, catalog_waves, report)

    return by_lower


async def _union_waves(
    db: AsyncSession,
    row: _DatasetRow,
    waves: list[str],
    report: DatasetCatalogSyncReport,
) -> None:
    merged = normalize_waves([*row.waves, *waves])
    if merged == row.waves:
        return
    dataset = await db.get(Dataset, row.id)
    assert dataset is not None
    dataset.waves = json.dumps(merged)
    row.waves = merged
    if row.name not in report.datasets_created and row.name not in report.datasets_updated:
        report.datasets_updated.append(row.name)


async def _dataset_for_collection(
    db: AsyncSession,
    by_lower: dict[str, _DatasetRow],
    collection: Collection,
    report: DatasetCatalogSyncReport,
) -> _DatasetRow:
    """The dataset a collection belongs to, holding the collection's wave.

    A card the pipeline no longer schedules still names a real collection, so
    its row is created here on first use. And a card whose schedule no longer
    lists the wave it was collected in gets that wave back — otherwise
    resolve_attribution_wave would refuse the group.
    """
    row = by_lower.get(collection.card.lower())
    if row is None:
        dataset, created_now = await _insert_or_get_dataset(collection.card, [collection.wave], db)
        row = _DatasetRow(id=dataset.id, name=dataset.name, waves=json.loads(dataset.waves))
        by_lower[collection.card.lower()] = row
        if created_now:
            report.datasets_created.append(row.name)
    await _union_waves(db, row, [collection.wave], report)
    scheduled = {name.lower(): waves for name, waves in PIPELINE_DATASETS}.get(
        collection.card.lower(), ()
    )
    note = f"{row.name} {collection.wave}"
    if collection.wave not in scheduled and note not in report.collected_outside_schedule:
        report.collected_outside_schedule.append(note)
    return row


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


def _check_collection(
    experiment: Experiment,
    filenames: list[str],
    collection: Collection,
    known_cards: list[str],
) -> tuple[str, str] | None:
    """Why this experiment must not be assigned to its listed collection, if anything.

    Returns (reason, detail), or None when every cross-check agrees.
    """
    if experiment.archived_at is not None:
        return "archived", ""

    cards = {match_card_name(filename, known_cards) for filename in filenames} - {None}
    if {card.lower() for card in cards} != {collection.card.lower()}:
        found = ", ".join(sorted(cards)) if cards else "no card"
        return "card_mismatch", f"uploads are exports of {found}; listed as {collection.card}"

    arm = experiment.assistance_method or "none"
    if arm != collection.arm:
        return "arm_mismatch", f"assistance method is {arm}; listed as {collection.arm}"

    tokens = wave_tokens([experiment.name, experiment.internal_name or "", *filenames])
    if tokens and tokens != {collection.wave}:
        found = ", ".join(sorted(tokens))
        return "wave_conflict", f"names carry {found}; listed as {collection.wave}"

    return None


async def _assign_experiments(
    db: AsyncSession,
    by_lower: dict[str, _DatasetRow],
    collections: Mapping[int, Collection],
    report: DatasetCatalogSyncReport,
) -> None:
    listed_ids = sorted(collections)
    experiments = (
        (
            await db.execute(
                select(Experiment)
                .where(or_(Experiment.group_id.is_(None), Experiment.id.in_(listed_ids)))
                .order_by(Experiment.id)
            )
        )
        .scalars()
        .all()
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

    found_ids = set(experiment_ids)
    report.manifest_missing = [eid for eid in listed_ids if eid not in found_ids]
    known_cards = [name for name, _ in PIPELINE_DATASETS] + sorted(
        {collection.card for collection in collections.values()}
        - {name for name, _ in PIPELINE_DATASETS}
    )

    def skip(experiment: Experiment, reason: str, detail: str = "") -> None:
        report.experiments_skipped.append(
            DatasetCatalogSkip(
                experiment_id=experiment.id,
                experiment_name=experiment.name,
                internal_name=experiment.internal_name,
                reason=reason,
                detail=detail,
            )
        )

    for experiment in experiments:
        collection = collections.get(experiment.id)
        if collection is None:
            skip(experiment, "not_in_manifest")
            continue
        if experiment.group_id is not None:
            skip(experiment, "already_grouped", f"group {experiment.group_id}")
            continue

        filenames = filenames_by_experiment.get(experiment.id, [])
        problem = _check_collection(experiment, filenames, collection, known_cards)
        if problem is not None:
            skip(experiment, *problem)
            continue

        dataset = await _dataset_for_collection(db, by_lower, collection, report)
        placed = await _get_or_create_group(db, dataset, collection.wave)
        if placed is None:
            skip(experiment, "group_name_conflict")
            continue
        group, created = placed
        if created:
            report.groups_created.append(group.name)
        experiment.group_id = group.id
        report.experiments_assigned.append(
            DatasetCatalogAssignment(
                experiment_id=experiment.id,
                experiment_name=experiment.name,
                internal_name=experiment.internal_name,
                dataset_name=dataset.name,
                arm=collection.arm,
                wave=collection.wave,
                group_id=group.id,
                group_name=group.name,
            )
        )


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

    # The collection's wave was added to the dataset just before this, so this
    # cannot refuse; it keeps group creation on the same rule the API uses.
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
