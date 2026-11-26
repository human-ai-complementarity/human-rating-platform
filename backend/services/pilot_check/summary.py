"""The latest run as the experiment page shows it and the launch gate reads it.

Both come from `summarise`, so the UI and the 409 (`gate.py`) cannot
disagree about why a launch needs acknowledging.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from config import PilotCheckSettings
from models import Experiment, PilotCheckRun
from schemas import PilotCheckSummary

from .runs import (
    compute_fingerprint,
    effective_status,
    fetch_latest_upload_ids,
    latest_runs,
    pilot_settings,
)


def _counts(findings: list[dict]) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for finding in findings:
        counts[finding.get("severity", "info")] += 1
    return counts


def summarise(
    run: PilotCheckRun | None,
    *,
    current_fingerprint: str,
    settings: PilotCheckSettings,
    now: datetime | None = None,
) -> PilotCheckSummary:
    """The state the experiment page shows and the launch gate reads.

    Anything but a fresh, passed run needs acknowledging (unless the gate is
    off): no run, a run still going, one that died, one the experiment has
    changed under, or one with a warning or error.
    """
    mode = settings.gate_mode
    if run is None:
        return PilotCheckSummary(
            state="not_run",
            needs_acknowledgement=mode != "off",
            reason="The pilot check has not been run.",
        )
    status = effective_status(run, now or datetime.now(UTC))
    counts = _counts(json.loads(run.findings or "[]"))
    if status == "running":
        state, reason = "running", "The pilot check is still running."
    elif status == "aborted":
        state, reason = "aborted", "The last pilot check did not finish."
    elif run.fingerprint != current_fingerprint:
        state, reason = "stale", "The experiment changed since the last pilot check."
    elif status == "passed":
        state, reason = "passed", None
    else:
        state = status
        reason = (
            f"The last pilot check found {counts['error']} error(s) and "
            f"{counts['warning']} warning(s)."
        )
    return PilotCheckSummary(
        state=state,
        run_id=run.id,
        created_at=run.created_at,
        finished_at=run.finished_at,
        counts=counts,
        needs_acknowledgement=mode != "off" and state != "passed",
        reason=reason,
    )


async def summaries_for(
    rows: list[tuple[Experiment, int]], db: AsyncSession
) -> dict[int, PilotCheckSummary]:
    """One summary per (experiment, question_count), in two queries."""
    settings = pilot_settings()
    ids = [experiment.id for experiment, _ in rows]
    runs = await latest_runs(ids, db)
    uploads = await fetch_latest_upload_ids(ids, db)
    now = datetime.now(UTC)
    return {
        experiment.id: summarise(
            runs.get(experiment.id),
            current_fingerprint=compute_fingerprint(
                experiment,
                question_count=question_count,
                latest_upload_id=uploads.get(experiment.id),
                config_sha256=settings.sha256(),
            ),
            settings=settings,
            now=now,
        )
        for experiment, question_count in rows
    }


async def summary_for(
    experiment: Experiment, *, question_count: int, db: AsyncSession
) -> PilotCheckSummary:
    return (await summaries_for([(experiment, question_count)], db))[experiment.id]
