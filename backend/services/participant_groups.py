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


async def ensure_participant_group_and_commit(
    experiment: Experiment,
    db: AsyncSession,
) -> str | None:
    """Return the Prolific participant group ID for `experiment`, creating it
    if needed and persisting the ID via `db.commit()`.

    Returns None when Prolific is disabled — callers treat that as "no group,
    proceed without exclusion." Name suffixed with `_and_commit` because
    callers must not rely on pending, uncommitted writes surviving this call.
    Existing callers either commit their own writes first (`start_session`)
    or only read before invoking (`_build_round_blocklist_group_ids`).

    Concurrency: the Prolific create call is made *before* acquiring the
    Experiment row lock. Two concurrent lazy-creates on the same experiment
    will both call Prolific and get separate group IDs, then serialize on the
    row lock — the first writes its ID, the second sees the winner's value
    and returns it, leaving its own group orphaned (empty, harmless). This
    avoids holding a row lock across an unbounded API call.

    The SELECT ... FOR UPDATE below uses `populate_existing=True` because the
    caller already loaded `experiment` earlier in this session, putting it in
    the identity map with a cached `prolific_participant_group_id=None`.
    Without `populate_existing`, SQLAlchemy would return the identity-mapped
    instance and discard the freshly-fetched row values — the "did the other
    side win?" check would keep reading the cached None, and the second writer
    would silently overwrite the winner's group ID (leaving the winner's
    already-added raters stranded in an orphaned group).
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

    group = await create_participant_group(
        settings=settings.prolific,
        name=participant_group_name(experiment),
    )

    # Serialize the DB write with a short row lock. Any concurrent caller that
    # also created a Prolific group will now see this row's ID and return it,
    # leaving its own group orphaned.
    locked = (
        await db.execute(
            select(Experiment).where(Experiment.id == experiment.id).with_for_update(),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    if locked.prolific_participant_group_id:
        return locked.prolific_participant_group_id
    locked.prolific_participant_group_id = group["id"]
    await db.commit()
    return group["id"]
