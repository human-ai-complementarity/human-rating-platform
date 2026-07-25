"""Experiment lifecycle helpers: lock enforcement, finish transition,
exclusion-target validation.

Grouped here so every entry point (update, upload, round create/update,
finish) applies the same rules against a single source of truth.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    ROUND_TERMINAL_STATUSES,
    Experiment,
    ExperimentRound,
    ExperimentStatus,
    ProlificStudyStatus,
)


def is_locked(experiment: Experiment) -> bool:
    """True once experiment-level config must be frozen (LAUNCH or FINISHED)."""
    return experiment.status != ExperimentStatus.DRAFT


def compute_attention_reason(
    *,
    status: ExperimentStatus,
    remaining_actions: int,
    round_statuses: list[ProlificStudyStatus],
) -> str | None:
    """Short reason an experiment has a pending admin action, or None if not.

    Mirrors the actionable states the detail view surfaces so the list can
    flag a row without loading round data per experiment:

      * An UNPUBLISHED round draft exists — publish it. (DRAFT or LAUNCH)
      * LAUNCH, every round closed, target not met — launch another round.
      * LAUNCH, every round closed, target met — mark the experiment finished.

    A round still collecting (any non-terminal published status) means "just
    wait", and a FINISHED experiment is terminal — neither is actionable.
    """
    if status == ExperimentStatus.FINISHED:
        return None
    if ProlificStudyStatus.UNPUBLISHED in round_statuses:
        return "A round draft is waiting to be published on Prolific."
    if status != ExperimentStatus.LAUNCH or not round_statuses:
        return None
    if any(s not in ROUND_TERMINAL_STATUSES for s in round_statuses):
        return None  # a round is still collecting — nothing to do yet
    if remaining_actions > 0:
        return "All rounds have closed but the rating target isn't met — launch another round."
    return "The rating target is met — mark the experiment finished."


def assert_editable(experiment: Experiment, action: str) -> None:
    """Reject `action` on an experiment whose config is locked."""
    if is_locked(experiment):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot {action}: experiment is {experiment.status}. "
                "Config is locked once the first main round is launched."
            ),
        )


async def assert_can_finish(experiment: Experiment, db: AsyncSession) -> None:
    """Precondition for LAUNCH -> FINISHED.

    Requires the experiment to be in LAUNCH and every round in a Prolific
    terminal status (`AWAITING_REVIEW` or `COMPLETED`). We check the DB rather
    than the cached round list because callers may not have it handy.
    """
    if experiment.status == ExperimentStatus.FINISHED:
        raise HTTPException(status_code=400, detail="Experiment is already finished.")
    if experiment.status != ExperimentStatus.LAUNCH:
        raise HTTPException(
            status_code=400,
            detail="Only launched experiments can be marked as finished.",
        )

    statuses = (
        (
            await db.execute(
                select(ExperimentRound.prolific_study_status).where(
                    ExperimentRound.experiment_id == experiment.id
                )
            )
        )
        .scalars()
        .all()
    )
    # Belt-and-braces: LAUNCH implies a main round exists, but we double-check
    # rather than assume the invariant is intact.
    if not statuses:
        raise HTTPException(
            status_code=400,
            detail="Cannot finish: no rounds have been run yet.",
        )
    non_terminal = [s for s in statuses if s not in ROUND_TERMINAL_STATUSES]
    if non_terminal:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot finish: close every round on Prolific first. "
                f"Non-terminal rounds: {len(non_terminal)}."
            ),
        )


async def validate_new_exclusion_targets(
    new_ids: list[int],
    *,
    previously_allowed_ids: list[int],
    db: AsyncSession,
) -> None:
    """Ensure any newly-added exclusion target is a FINISHED experiment.

    IDs that were already present in `previously_allowed_ids` are grandfathered
    — they were legal when set, and we don't want a status change on the target
    to break an unrelated update of the referencing round. Only *new* IDs
    (present in `new_ids` but not in `previously_allowed_ids`) are checked.
    """
    added = set(new_ids) - set(previously_allowed_ids)
    if not added:
        return

    experiments_by_id = {
        exp.id: exp
        for exp in (await db.execute(select(Experiment).where(Experiment.id.in_(added)))).scalars()
    }
    invalid: list[str] = []
    for exp_id in added:
        exp = experiments_by_id.get(exp_id)
        if exp is None:
            invalid.append(f"{exp_id} (missing)")
        elif exp.status != ExperimentStatus.FINISHED:
            invalid.append(f"{exp_id} ({exp.status})")

    if invalid:
        raise HTTPException(
            status_code=400,
            detail=(
                "Exclusion targets must be finished experiments. "
                f"Rejected: {', '.join(sorted(invalid))}."
            ),
        )
