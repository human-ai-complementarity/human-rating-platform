"""The launch gate: ask before a study is created past the pilot check.

It never refuses. A judge will always find something to fix, so the most
the check does is make the admin confirm and record who did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import Experiment
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
    no mode that refuses an acknowledged launch.
    """
    summary = await summary_for(experiment, question_count=question_count, db=db)
    if not summary.needs_acknowledgement:
        return PilotCheckGate(summary=summary, acknowledged_by=None)
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
