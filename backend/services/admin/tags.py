"""Global experiment tags: normalization, attachment, and usage-ranked listing.

Tags are the free-form leftover after dataset/wave/method became structure —
project, client, one-offs. A "needs-review" tag on one experiment is the same
row as on another, so the create flow can suggest existing tags ranked by
usage and the list can filter by them.
"""

from __future__ import annotations

from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Experiment, ExperimentTag, Tag
from schemas import TagResponse


def normalize_tag_names(names: list[str]) -> list[str]:
    """Trim, collapse internal whitespace, and dedupe case-insensitively.

    The first-seen casing wins within the input list; order is preserved.
    Empty results are dropped.
    """
    seen: dict[str, str] = {}
    for raw in names:
        cleaned = " ".join(raw.split())
        if cleaned and cleaned.lower() not in seen:
            seen[cleaned.lower()] = cleaned
    return list(seen.values())


async def get_or_create_tags(names: list[str], db: AsyncSession) -> list[Tag]:
    """Resolve tag names to rows, creating missing ones. Does not commit.

    Matching is case-insensitive; an existing tag's stored casing is kept
    even when the caller typed a different one.
    """
    normalized = normalize_tag_names(names)
    if not normalized:
        return []

    existing = (
        (
            await db.execute(
                select(Tag).where(func.lower(Tag.name).in_([name.lower() for name in normalized]))
            )
        )
        .scalars()
        .all()
    )
    by_lower = {tag.name.lower(): tag for tag in existing}

    resolved: list[Tag] = []
    for name in normalized:
        tag = by_lower.get(name.lower())
        if tag is None:
            tag = await _insert_or_get_tag(name, db)
            by_lower[name.lower()] = tag
        resolved.append(tag)
    return resolved


async def _insert_or_get_tag(name: str, db: AsyncSession) -> Tag:
    """Insert `name`, or return the row that won a concurrent insert.

    Two requests can both miss the SELECT and both INSERT; the unique index
    `uq_tags_name_lower` rejects the loser. Sharing a tag is the intended
    case, so recover by re-reading rather than 409. A savepoint keeps the
    rest of the caller's transaction (the new experiment, etc.) intact.
    """
    try:
        async with db.begin_nested():
            tag = Tag(name=name)
            db.add(tag)
            await db.flush()
            return tag
    except IntegrityError:
        existing = (
            await db.execute(select(Tag).where(func.lower(Tag.name) == name.lower()))
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing


async def set_experiment_tags(
    experiment_id: int,
    names: list[str],
    db: AsyncSession,
) -> list[str]:
    """Replace an experiment's tag set with `names`. Does not commit.

    Unused tag rows stay in the vocabulary as zero-usage suggestions.

    Returns the attached tag names, alphabetical (response order).
    """
    tags = await get_or_create_tags(names, db)

    await db.execute(delete(ExperimentTag).where(ExperimentTag.experiment_id == experiment_id))
    for tag in tags:
        db.add(ExperimentTag(experiment_id=experiment_id, tag_id=tag.id))
    await db.flush()

    return sorted((tag.name for tag in tags), key=str.lower)


async def fetch_tag_names_for_experiment(experiment_id: int, db: AsyncSession) -> list[str]:
    """Tag names for one experiment; same join/sort as the batch helper."""
    return (await fetch_tag_names_by_experiment([experiment_id], db)).get(experiment_id, [])


async def fetch_tag_names_by_experiment(
    experiment_ids: list[int], db: AsyncSession
) -> dict[int, list[str]]:
    """Tag names keyed by experiment id, for the list endpoint (one query)."""
    result: dict[int, list[str]] = {eid: [] for eid in experiment_ids}
    if not experiment_ids:
        return result
    rows = (
        await db.execute(
            select(ExperimentTag.experiment_id, Tag.name)
            .join(Tag, Tag.id == ExperimentTag.tag_id)
            .where(ExperimentTag.experiment_id.in_(experiment_ids))
        )
    ).all()
    for experiment_id, name in rows:
        result.setdefault(experiment_id, []).append(name)
    for names in result.values():
        names.sort(key=str.lower)
    return result


async def list_tags(db: AsyncSession) -> list[TagResponse]:
    """All tags with usage counts, most-used first (name breaks ties).

    Usage counts *active* experiments only: archived work doesn't reflect
    current conventions, and deleted experiments drop out via FK cascade.
    Zero-usage tags still appear, ranked last.
    """
    active_usage = func.count(Experiment.id)
    rows = (
        await db.execute(
            select(Tag.name, active_usage.label("usage_count"))
            .outerjoin(ExperimentTag, ExperimentTag.tag_id == Tag.id)
            .outerjoin(
                Experiment,
                and_(
                    Experiment.id == ExperimentTag.experiment_id,
                    Experiment.archived_at.is_(None),
                ),
            )
            .group_by(Tag.id, Tag.name)
            .order_by(active_usage.desc(), func.lower(Tag.name))
        )
    ).all()
    return [TagResponse(name=name, usage_count=int(count)) for name, count in rows]
