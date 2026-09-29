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
# rater actually get instructions, framing and a pinned model?" — is a question
# every experiment can answer, and holds ungrouped ones to the same bar instead
# of blocking them wholesale or waving them through.
_ROW_REQUIRED_TEXT: tuple[tuple[str, str], ...] = (
    ("description", "rater instructions"),
    ("human_prompt_prefix", "prompt prefix"),
    ("human_prompt_suffix", "prompt suffix"),
    ("internal_name", "internal study name"),
)


def experiment_launch_blockers(experiment: Experiment) -> list[str]:
    """What still stops this experiment launching a study, in report order.

    `name` is not checked: it is required at create, so it is always present.
    The model is only required when assistance is actually on — a control arm
    never calls an LLM. It arrives with the upload, stamped into the export by
    the pipeline from the wave's comparable arm, so a wave that declares no
    comparable arm lands here and is asked for one. It is present when
    `pinned_model` finds one for the current method, the resolver's own test,
    so the gate and the model that runs cannot disagree.
    """
    blockers = [
        label for attr, label in _ROW_REQUIRED_TEXT if not (getattr(experiment, attr) or "").strip()
    ]
    if experiment.assistance_method != "none":
        params = json.loads(experiment.assistance_params) if experiment.assistance_params else {}
        if not pinned_model(params, experiment.assistance_method):
            blockers.append("assistance model")
    return blockers


# Blockers whose value is part of the pipeline export's metadata, applied at
# upload (`_apply_meta_to_experiment`). The internal study name is the one
# gated field the dataset card supplies instead — and only when an experiment
# is created, so editing the card afterwards cannot fix an existing one.
_FROM_UPLOAD_META = ("rater instructions", "prompt prefix", "prompt suffix", "assistance model")


def launch_blocker_fixes(blockers: list[str]) -> list[str]:
    """The fix for each blocker, grouped by where its value comes from."""
    fixes: list[str] = []
    from_upload = [b for b in blockers if b in _FROM_UPLOAD_META]
    if from_upload:
        them = "it" if len(from_upload) == 1 else "them"
        listed = ", ".join(from_upload)
        via_api = (
            " The assistance model has no field in the UI. The export declares it per"
            " method under `assistance_models`; to set it by hand, PATCH"
            " assistance_params.assistance_models.<method>."
            if "assistance model" in from_upload
            else ""
        )
        fixes.append(
            f"{listed[0].upper()}{listed[1:]}: part of the pipeline export's "
            f"metadata, applied at upload. If the upload lacked {them}, set {them} on "
            "this experiment directly; uploading the file again would add its "
            f"questions twice.{via_api}"
        )
    if "internal study name" in blockers:
        fixes.append(
            "Internal study name: copied from the dataset card's template only when "
            "an experiment is created, so set it on this experiment."
        )
    return fixes


def assert_launch_ready(experiment: Experiment) -> None:
    """Refuse to create a study for an experiment that isn't fully onboarded.

    Bites at first study creation — the last moment before a Prolific study
    object and real money exist. Not at experiment create (per Joshua on #96,
    headroom analysis must keep working on an unfinished card) and not at
    publish, where refusing would leave an orphan draft study to discard.
    """
    blockers = experiment_launch_blockers(experiment)
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
