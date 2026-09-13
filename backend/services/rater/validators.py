from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import Rater
from services.session_policy import SessionPolicy

logger = logging.getLogger(__name__)


def validate_rating_confidence(confidence: int) -> None:
    if confidence < 1 or confidence > 5:
        raise HTTPException(status_code=400, detail="Confidence must be between 1 and 5")


def validate_question_belongs_to_rater_experiment(
    *,
    question_experiment_id: int,
    rater_experiment_id: int,
) -> None:
    if question_experiment_id != rater_experiment_id:
        raise HTTPException(status_code=400, detail="Question does not belong to this experiment")


def validate_existing_rater_can_resume(existing_rater: Rater, policy: SessionPolicy) -> None:
    # Judged against the hard deadline so a rater who refreshes while finishing
    # their last question inside the grace window gets back in. They still
    # won't be served anything new — get_next_question gates on the deadline.
    if datetime.now(UTC) > policy.hard_deadline(existing_rater.session_start):
        raise HTTPException(
            status_code=403,
            detail="You have already completed a session for this experiment",
        )
    if not existing_rater.is_active:
        raise HTTPException(
            status_code=403,
            detail="You have already completed a session for this experiment",
        )


async def validate_rater_can_be_served(
    rater: Rater, db: AsyncSession, policy: SessionPolicy
) -> None:
    """Gate on the soft deadline: no new questions once the clock runs out.

    Deliberately does NOT mark the rater inactive — that is what ends their
    ability to submit, and during the grace window they still have a question
    in front of them to finish. Closing the session out is
    `expire_rater_if_past_grace`'s job.
    """
    if datetime.now(UTC) <= policy.deadline(rater.session_start):
        return

    await expire_rater_if_past_grace(rater, db, policy)
    raise HTTPException(status_code=403, detail="Session expired")


async def validate_rater_session_not_over(
    rater: Rater, db: AsyncSession, policy: SessionPolicy
) -> None:
    """Gate on the hard deadline: past it nothing is accepted at all.

    Guards both submitting and re-serving, because the grace window exists to
    let a rater finish the question they already hold — and a reload during
    grace has to be able to fetch it back.
    """
    if datetime.now(UTC) <= policy.hard_deadline(rater.session_start):
        return

    await expire_rater_if_past_grace(rater, db, policy)
    raise HTTPException(status_code=403, detail="Session expired")


async def expire_rater_if_past_grace(rater: Rater, db: AsyncSession, policy: SessionPolicy) -> None:
    """Close a session out once even the grace window has passed.

    Expiry stays lazy — nothing sweeps — so this runs off whichever rater call
    happens to notice first.
    """
    if datetime.now(UTC) <= policy.hard_deadline(rater.session_start):
        return
    if not rater.is_active:
        return

    logger.warning(
        "Rater session expired",
        extra={
            "attributes": {
                "rater_id": rater.id,
                "experiment_id": rater.experiment_id,
            }
        },
    )
    rater.is_active = False
    rater.session_end = datetime.now(UTC)
    await db.commit()


def validate_rater_marked_active(rater: Rater) -> None:
    if not rater.is_active:
        raise HTTPException(status_code=403, detail="Session expired")
