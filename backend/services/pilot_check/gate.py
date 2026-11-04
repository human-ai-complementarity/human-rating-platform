"""The launch gate: refuse a study creation the pilot check should stop."""

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
    # Set when the admin launched past a check the gate would have stopped.
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
    """Refuse a study creation the pilot check should stop (docs/pilot-check.md).

    Runs after `assert_launch_ready`: hard blockers stay hard. With the gate in
    "warn" mode a missing, stale, running or failing check is a 409 carrying the
    summary, unless the request acknowledges it; "block" refuses regardless;
    "off" records nothing.
    """
    summary = await summary_for(experiment, question_count=question_count, db=db)
    if not summary.needs_acknowledgement:
        return PilotCheckGate(summary=summary, acknowledged_by=None)
    if summary.gate_mode == "block":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pilot_check_blocked",
                "message": f"{summary.reason} Run the pilot check and fix what it finds "
                "before creating a study.",
                "pilot_check": summary.model_dump(mode="json"),
            },
        )
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
