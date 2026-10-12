"""Experiment lifecycle helpers: lock enforcement, finish transition,
exclusion-target validation.

Grouped here so every entry point (update, upload, round create/update,
finish) applies the same rules against a single source of truth.
"""

from __future__ import annotations

import json

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
from services.assistance.model_resolution import pinned_model

from .queries import fetch_total_questions_for_experiment


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


# --- Launch readiness (#96) ----------------------------------------------
# Read off the *experiment row*, not the dataset card. Two reasons: the card
# snapshots onto the row at create, so the card's current state has nothing to
# do with what raters will see; and #84 deliberately keeps ungrouped
# experiments valid, so they have no card at all. Asking the row — "will this
# rater actually get instructions and a pinned model?" — is a question every
# experiment can answer, and holds ungrouped ones to the same bar instead of
# blocking them wholesale or waving them through.
#
# The prompt prefix and suffix are optional: a dataset may frame its questions
# with one, both or neither.
_ROW_REQUIRED_TEXT: tuple[tuple[str, str], ...] = (
    ("description", "rater instructions"),
    ("internal_name", "internal study name"),
)


def experiment_launch_blockers(experiment: Experiment, *, question_count: int) -> list[str]:
    """What still stops this experiment launching a study, in report order.

    `question_count` is `ExperimentResponse.question_count`, which skips parent
    rows (header-only, never served). Publishing with none would move the
    experiment to LAUNCH, which blocks uploads. Reported first, since the
    upload also brings the rater instructions and model.

    `name` is not checked: it is required at create, so it is always present.
    The model is only required when assistance is actually on — a control arm
    never calls an LLM. It arrives with the upload, stamped into the export by
    the pipeline from the wave's comparable arm, so a wave that declares no
    comparable arm lands here and is asked for one. It is present when
    `pinned_model` finds one for the current method, the resolver's own test,
    so the gate and the model that runs cannot disagree.
    """
    blockers = [] if question_count > 0 else ["questions"]
    blockers += [
        label for attr, label in _ROW_REQUIRED_TEXT if not (getattr(experiment, attr) or "").strip()
    ]
    if experiment.assistance_method != "none":
        params = json.loads(experiment.assistance_params) if experiment.assistance_params else {}
        if not pinned_model(params, experiment.assistance_method):
            blockers.append("assistance model")
    return blockers


# The fix for each blocker, by where its value comes from. The rater
# instructions and model are part of the pipeline export's metadata, applied
# at upload (`_apply_meta_to_experiment`); the internal study name comes from
# the dataset card, and only when an experiment is created, so editing the
# card afterwards cannot fix an existing one. Served to the admin UI too
# (`ExperimentResponse.launch_blocker_fixes`), so the wording names its fields.
_LAUNCH_BLOCKER_FIXES: dict[str, str] = {
    "questions": (
        "Questions: upload questions on the Questions tab. The pipeline export's metadata "
        "brings the rater instructions and assistance model with them, so upload before "
        "setting either by hand."
    ),
    "rater instructions": (
        "Rater instructions: part of the pipeline export's metadata, applied at upload. "
        "If the upload lacked them, set them under Instructions & prompts; uploading "
        "the file again would add its questions twice."
    ),
    "internal study name": (
        "Internal study name: copied from the dataset card's template only when an "
        "experiment is created, so set it on this experiment with the pencil by its title."
    ),
    "assistance model": (
        "Assistance model: part of the pipeline export's metadata, per method under "
        "assistance_models, applied at upload. It has no field in the admin UI; to set "
        "it by hand, PATCH /api/admin/experiments/<id> with "
        "assistance_params.assistance_models.<method>."
    ),
}


def launch_blocker_fixes(blockers: list[str]) -> list[str]:
    """The fix for each blocker, in the blockers' order."""
    return [_LAUNCH_BLOCKER_FIXES[blocker] for blocker in blockers]


async def assert_launch_ready(experiment: Experiment, db: AsyncSession) -> None:
    """Refuse to create a study for an experiment that isn't fully onboarded.

    Bites at first study creation — the last moment before a Prolific study
    object and real money exist. Not at experiment create (per Joshua on #96,
    headroom analysis must keep working on an unfinished card) and not at
    publish, where refusing would leave an orphan draft study to discard.
    """
    question_count = await fetch_total_questions_for_experiment(experiment.id, db)
    blockers = experiment_launch_blockers(experiment, question_count=question_count)
    if blockers:
        raise HTTPException(
            status_code=400,
            detail=" ".join(
                [
                    f"Cannot launch: this experiment is missing {', '.join(blockers)}.",
                    *launch_blocker_fixes(blockers),
                ]
            ),
        )


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
