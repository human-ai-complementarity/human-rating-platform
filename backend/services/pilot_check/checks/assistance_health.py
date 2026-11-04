"""Across the whole sample: is the method working at all, and fast enough."""

from __future__ import annotations

import json
import statistics

from ..config import Severity
from .base import Check, CheckContext, Finding


class AssistanceHealth(Check):
    name = "assistance_health"

    async def run(self, ctx: CheckContext, severity: Severity) -> list[Finding]:
        if ctx.probes is None:
            return [self.skipped("the experiment has no assistance method")]
        if not ctx.probes:
            return [self.skipped("no questions were sampled")]
        softer: Severity = "warning" if severity == "error" else severity
        limits = ctx.config.assistance
        findings: list[Finding] = []
        total = len(ctx.probes)
        failed = [probe for probe in ctx.probes if not probe.succeeded]
        share = len(failed) / total
        if share > limits.max_failure_share:
            reasons = sorted({probe.failure_reason or "unknown" for probe in failed})
            findings.append(
                self.finding(
                    severity,
                    f"{len(failed)} of {total} sampled questions got no assistance "
                    f"({', '.join(reasons)}).",
                    detail=next((p.failure_detail for p in failed if p.failure_detail), None),
                )
            )
        latencies = [probe.latency_ms for probe in ctx.probes if probe.succeeded]
        if latencies:
            median = statistics.median(latencies) / 1000
            if median > limits.max_median_latency_seconds:
                findings.append(
                    self.finding(
                        softer,
                        f"Median assistance latency is {median:.1f}s, over the "
                        f"{limits.max_median_latency_seconds:.0f}s limit; raters will wait.",
                    )
                )
        succeeded = [probe for probe in ctx.probes if probe.succeeded]
        if len(succeeded) > 1:
            shapes = {json.dumps(probe.payload, sort_keys=True) for probe in succeeded}
            if len(shapes) == 1:
                findings.append(
                    self.finding(
                        severity,
                        f"All {len(succeeded)} successful questions got byte-identical "
                        "assistance; the method is not reading the question.",
                    )
                )
        if not findings:
            findings.append(
                self.finding(
                    "info",
                    f"{len(succeeded)} of {total} sampled questions got assistance"
                    + (
                        f", median {statistics.median(latencies) / 1000:.1f}s."
                        if latencies
                        else "."
                    ),
                )
            )
        return findings
