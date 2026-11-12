"""The launch gate: ask before a study is created past the pilot check.

It never refuses. A judge will always find something to fix, so the most
the check does is make the admin confirm and record who did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Experiment, ExperimentRound
from schemas import PilotCheckSummary

from .summary import summary_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PilotCheckGate:
    """What a launch records about the check it was made against."""

    summary: PilotCheckSummary
    # Set when the admin confirmed a launch past a missing, stale or flagged check.
    acknowledged_by: str | None

    @property
    def run_id(self) -> int | None:
        return self.summary.run_id


async def gate_for(
    experiment: Experiment,
    db: AsyncSession,
    *,
    acknowledged: bool,
    admin_email: str | None,
    question_count: int,
) -> PilotCheckGate:
    """Ask before a study is created past the pilot check (docs/pilot-check.md).

    Runs after `assert_launch_ready`: hard blockers stay hard. With the gate in
    "warn" mode a missing, stale, running or failing check is a 409 carrying the
    summary until the request acknowledges it; "off" records nothing. There is
    no mode that refuses an acknowledged launch, and a flagged run is asked
    about once: a later round against the same run, with nothing changed under
    it, goes through carrying the earlier acknowledger.
    """
    summary = await summary_for(experiment, question_count=question_count, db=db)
    if not summary.needs_acknowledgement:
        return PilotCheckGate(summary=summary, acknowledged_by=None)
    earlier = await _acknowledged_earlier(experiment.id, summary, db)
    if earlier is not None:
        return PilotCheckGate(summary=summary, acknowledged_by=earlier)
    if not acknowledged:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pilot_check",
                "message": f"{summary.reason} Run the pilot check first, or acknowledge "
                "and launch anyway.",
                "pilot_check": summary.model_dump(mode="json"),
            },
        )
    logger.warning(
        "Launching past the pilot check",
        extra={
            "attributes": {
                "experiment_id": experiment.id,
                "pilot_check_state": summary.state,
                "acknowledged_by": admin_email,
            }
        },
    )
    return PilotCheckGate(summary=summary, acknowledged_by=admin_email or "unknown")


async def _acknowledged_earlier(
    experiment_id: int, summary: PilotCheckSummary, db: AsyncSession
) -> str | None:
    """Who acknowledged this same run on an earlier round.

    Only a fresh run with findings pins what was acknowledged: the same run
    id, still fresh, means nothing changed since. A stale run does not (the
    experiment may change again between rounds), an aborted one checked
    nothing, and a missing one has no id to key on; each is asked about again.
    """
    if summary.state not in ("warnings", "failed") or summary.run_id is None:
        return None
    return await db.scalar(
        select(ExperimentRound.pilot_check_acknowledged_by)
        .where(
            ExperimentRound.experiment_id == experiment_id,
            ExperimentRound.pilot_check_run_id == summary.run_id,
            ExperimentRound.pilot_check_acknowledged_by.is_not(None),
        )
        .order_by(ExperimentRound.id)
        .limit(1)
    )
