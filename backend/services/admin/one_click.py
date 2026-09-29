"""One-click launch from a dataset card (#96).

"One click" honestly means *create the pilot draft*, not "dataset to live
study". Two things keep it there deliberately:

- Publishing stays a separate action, because it spends money.
- Questions still arrive by upload, so this refuses rather than creating a
  study with nothing to rate.

Only a *complete* dataset launches this way: launch-ready, with the
economics (estimated completion time and reward) on its card too. Those stay
optional at onboarding, so a dataset's first study usually goes through the
pilot form, which asks for them.

The pilot excludes the participants of the dataset's other experiments, which
the pilot form would otherwise ask the admin to pick by hand.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Dataset, Experiment, ExperimentGroup, Question
from schemas import (
    ExperimentRef,
    ExperimentRoundResponse,
    OneClickLaunchPreview,
    OneClickLaunchRequest,
    PilotStudyCreate,
)
from .dataset_card import card_readiness, card_values_from_row
from .queries import fetch_experiment_or_404
from .rounds import run_pilot_study
from .status import assert_launch_ready

DEFAULT_PILOT_PLACES = 5


async def dataset_exclusions(experiment: Experiment, db: AsyncSession) -> list[ExperimentRef]:
    """The experiments whose participants a one-click pilot excludes.

    Every other experiment on the same dataset, through any of its groups and
    so any wave, in any status, whether or not it has run a study yet.
    Prolific participant groups are dynamic: a sibling with no raters still
    gets its (empty) group on the blocklist, and its raters join that group as
    they arrive. So arms piloted one after another keep each other out both
    ways. An arm created after this pilot isn't on its list; only its own
    pilot excludes this one.
    """
    if experiment.group_id is None:
        return []
    dataset_id = (
        select(ExperimentGroup.dataset_id)
        .where(ExperimentGroup.id == experiment.group_id)
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Experiment.id, Experiment.name)
        .join(ExperimentGroup, Experiment.group_id == ExperimentGroup.id)
        .where(ExperimentGroup.dataset_id == dataset_id, Experiment.id != experiment.id)
        .order_by(Experiment.id)
    )
    return [ExperimentRef(id=row.id, name=row.name) for row in rows]


async def preview_launch_from_card(experiment_id: int, db: AsyncSession) -> OneClickLaunchPreview:
    """What `launch_from_card` would exclude, for the admin to see first."""
    experiment = await fetch_experiment_or_404(experiment_id, db)
    return OneClickLaunchPreview(excluded_experiments=await dataset_exclusions(experiment, db))


def _blurb(card: dict[str, object]) -> str | None:
    """The Prolific-facing study description.

    Distinct from the rater guide: this is what a prospective participant
    reads in the study listing, where the full limitations blob reads badly.
    """
    return card.get("study_blurb")  # type: ignore[return-value]


async def launch_from_card(
    experiment_id: int,
    payload: OneClickLaunchRequest,
    db: AsyncSession,
) -> ExperimentRoundResponse:
    experiment = await fetch_experiment_or_404(experiment_id, db)

    question_count = (
        await db.execute(
            select(func.count())
            .select_from(Question)
            .where(Question.experiment_id == experiment_id)
        )
    ).scalar_one()
    if not question_count:
        raise HTTPException(
            status_code=400,
            detail="Cannot launch: upload questions for this experiment first.",
        )

    # After the question check, not before. The rater instructions and prompt
    # framing now arrive WITH the upload (the pipeline stamps them into the
    # exported file), so on an experiment with nothing uploaded the readiness
    # error would both misdiagnose the problem and send the admin in a circle:
    # the way to supply what it asks for is to upload.
    assert_launch_ready(experiment)

    if experiment.group_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "One-click launch needs a dataset card, so the experiment must belong to a "
                "group. Use the pilot form instead."
            ),
        )
    group = await db.get(ExperimentGroup, experiment.group_id)
    dataset = await db.get(Dataset, group.dataset_id) if group else None
    card = card_values_from_row(dataset) if dataset else {}

    readiness = card_readiness(card)
    if not readiness.complete:
        missing = ", ".join(name.replace("_", " ") for name in readiness.missing_for_complete)
        raise HTTPException(
            status_code=400,
            detail=(
                f"One-click launch needs a complete dataset card, and this one is missing "
                f"{missing}. Fill them in on the dataset card, or use the pilot form."
            ),
        )

    description = _blurb(card)
    excluded_ids = [ref.id for ref in await dataset_exclusions(experiment, db)]
    pilot = PilotStudyCreate(
        description=str(description),
        estimated_completion_time=int(card["estimated_completion_time"]),  # type: ignore[arg-type]
        reward=int(card["reward"]),  # type: ignore[arg-type]
        pilot_places=payload.places or DEFAULT_PILOT_PLACES,
        excluded_experiment_ids=excluded_ids,
        **(
            {"study_label": card["study_label"]}  # type: ignore[dict-item]
            if card.get("study_label")
            else {}
        ),
        **({"screeners": card["screeners"]} if card.get("screeners") is not None else {}),  # type: ignore[dict-item]
    )
    # The pilot form only accepts FINISHED experiments as new exclusions; these
    # are chosen here, and a sibling still collecting is exactly the one to block.
    return await run_pilot_study(experiment_id, pilot, db, preapproved_exclusion_ids=excluded_ids)
