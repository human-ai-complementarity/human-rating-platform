"""Across the whole sample: is the method working at all, and fast enough."""

from __future__ import annotations

import json
import statistics

from .base import CheckContext, CheckSpec, Finding, Severity, skipped, soften


NAME = "assistance_health"


async def run(ctx: CheckContext, severity: Severity) -> list[Finding]:
    if ctx.probes is None:
        return [skipped(NAME, "the experiment has no assistance method")]
    if not ctx.probes:
        return [skipped(NAME, "no questions were sampled")]
    softer = soften(severity)
    findings: list[Finding] = []
    total = len(ctx.probes)
    failed = [probe for probe in ctx.probes if not probe.succeeded]
    if failed:
        reasons = sorted({probe.failure_reason or "unknown" for probe in failed})
        findings.append(
            Finding(
                NAME,
                severity,
                f"{len(failed)} of {total} sampled questions got no assistance "
                f"({', '.join(reasons)}).",
                detail=next((p.failure_detail for p in failed if p.failure_detail), None),
            )
        )
    latencies = [probe.latency_ms for probe in ctx.probes if probe.succeeded]
    if latencies:
        median = statistics.median(latencies) / 1000
        if median > ctx.settings.max_median_latency_seconds:
            findings.append(
                Finding(
                    NAME,
                    softer,
                    f"Median assistance latency is {median:.1f}s, over the "
                    f"{ctx.settings.max_median_latency_seconds:.0f}s limit; raters will wait.",
                )
            )
    succeeded = [probe for probe in ctx.probes if probe.succeeded]
    if len(succeeded) > 1:
        shapes = {json.dumps(probe.payload, sort_keys=True) for probe in succeeded}
        if len(shapes) == 1:
            findings.append(
                Finding(
                    NAME,
                    severity,
                    f"All {len(succeeded)} successful questions got byte-identical "
                    "assistance; the method is not reading the question.",
                )
            )
    if not findings:
        findings.append(
            Finding(
                NAME,
                "info",
                f"{len(succeeded)} of {total} sampled questions got assistance"
                + (f", median {statistics.median(latencies) / 1000:.1f}s." if latencies else "."),
            )
        )
    return findings


CHECK = CheckSpec(NAME, "Assistance health", run, "error", needs_assistance=True)
