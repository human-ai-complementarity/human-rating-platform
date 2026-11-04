"""The #96 launch gate's blockers, repeated so a run's report stands alone."""

from __future__ import annotations

from ..config import Severity
from .base import Check, CheckContext, Finding


class LaunchBlockers(Check):
    name = "launch_blockers"

    async def run(self, ctx: CheckContext, severity: Severity) -> list[Finding]:
        # Imported here: services.admin imports this package for the summary
        # on ExperimentResponse, so a module-level import would be circular.
        from services.admin.status import experiment_launch_blockers, launch_blocker_fixes

        blockers = experiment_launch_blockers(ctx.experiment, question_count=ctx.question_count)
        fixes = launch_blocker_fixes(blockers)
        return [
            self.finding(severity, f"The experiment is missing {blocker}.", detail=fix)
            for blocker, fix in zip(blockers, fixes)
        ]
