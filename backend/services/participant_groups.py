"""Shared helpers for the Prolific participant-group-per-experiment scheme.

Each Experiment gets one Prolific participant group. Raters are added to their
experiment's group on start_session, and later experiments reference that group
via `excluded_experiment_ids` to blocklist prior participants.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import Experiment
from services.admin.prolific import create_participant_group

logger = logging.getLogger(__name__)


def _slugify_for_prolific(value: str) -> str:
    out: list[str] = []
    prev_hyphen = False
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
            prev_hyphen = False
        elif not prev_hyphen:
            out.append("-")
            prev_hyphen = True
    return "".join(out).strip("-")[:40] or "experiment"


def participant_group_name(experiment: Experiment) -> str:
    """Group name as it appears in the Prolific researcher UI.

    Format: `[{env_label}-]exp-{id}-{slug}`. The env prefix keeps dev-created
    groups distinguishable from prod ones when they share a Prolific project.
    """
    settings = get_settings()
    prefix = settings.prolific.env_label.strip()
    parts = [prefix] if prefix else []
    parts.extend(["exp", str(experiment.id), _slugify_for_prolific(experiment.name)])
    return "-".join(parts)


async def ensure_participant_group(
    experiment: Experiment,
    db: AsyncSession,
) -> str | None:
    """Return the Prolific participant group ID for `experiment`, creating it if
    needed and persisting the ID.

    Returns None when Prolific is disabled — callers treat that as "no group,
    proceed without exclusion."

    Warning: on the create path this issues `db.commit()` to persist the new
    group ID. Callers must not rely on pending, uncommitted writes surviving
    this call. Existing callers either commit their own writes first
    (`start_session`) or only read before invoking (`_build_round_blocklist_group_ids`).
    """
    if experiment.prolific_participant_group_id:
        return experiment.prolific_participant_group_id

    settings = get_settings()
    if not settings.prolific.enabled:
        return None
    if not settings.prolific.project_id:
        # Group create requires a project_id — degrade gracefully rather than
        # failing every round launch. Cross-experiment exclusion is a no-op
        # until an admin sets PROLIFIC__PROJECT_ID.
        return None

    # Serialize the lazy-create path with a row lock. Without it, two
    # concurrent start_session calls on a group-less experiment can each
    # create a Prolific group, and the losing DB commit leaves an orphan
    # group on Prolific with a rater silently attached to it. Lock is
    # released on the commit below (or on exception).
    locked = (
        await db.execute(select(Experiment).where(Experiment.id == experiment.id).with_for_update())
    ).scalar_one()
    if locked.prolific_participant_group_id:
        return locked.prolific_participant_group_id

    group = await create_participant_group(
        settings=settings.prolific,
        name=participant_group_name(experiment),
    )
    locked.prolific_participant_group_id = group["id"]
    await db.commit()
    return group["id"]
