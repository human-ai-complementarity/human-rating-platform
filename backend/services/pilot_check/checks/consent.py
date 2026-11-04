"""Is the consent statement raters will be shown the right one, and whole."""

from __future__ import annotations

from fastapi import HTTPException

from services.terms import (
    PLACEHOLDER_LABEL,
    find_placeholders,
    format_session_length,
    statement_label,
    study_requirements,
    validate_terms_config,
)
from session_policy import resolve_session_policy

from ..config import Severity
from .base import Check, CheckContext, Finding


class ConsentConfigured(Check):
    name = "consent_configured"

    async def run(self, ctx: CheckContext, severity: Severity) -> list[Finding]:
        softer: Severity = "warning" if severity == "error" else severity
        experiment = ctx.experiment
        findings: list[Finding] = []

        # Field-level rules first: they hold whether or not the source is readable.
        try:
            validate_terms_config(experiment)
            needs = study_requirements(experiment)
        except HTTPException as exc:
            findings.append(self.finding(severity, str(exc.detail)))
            needs = None

        if ctx.terms_error:
            findings.append(
                self.finding(
                    severity,
                    "The consent statement could not be read, so raters would get an error "
                    "instead of a consent screen.",
                    detail=ctx.terms_error,
                )
            )
            return findings
        terms = ctx.terms
        if terms is None:
            findings.append(self.finding(severity, "No consent statement was loaded for this run."))
            return findings
        consent = terms.consent
        if consent.label == PLACEHOLDER_LABEL:
            findings.append(
                self.finding(
                    severity,
                    "TERMS__SOURCE_URL is unset, so raters would be shown the placeholder "
                    "'consent statement not configured' text instead of a consent statement.",
                )
            )
            return findings

        # The statement the flags select. A live read always picks the right
        # one; a pinned experiment keeps the version it went live with, so a
        # flag changed after the first publish is what this catches.
        if needs is not None and consent.document != needs.consent_document:
            flags = ", ".join(needs.flags) or "none"
            findings.append(
                self.finding(
                    severity,
                    f"Raters would be shown the {consent.ref} consent statement, but the study's "
                    f"flags ({flags}) call for the {statement_label(needs.consent_document)} one. "
                    "The pinned statement predates the flag change.",
                )
            )
        if needs is not None and needs.debrief_document and terms.debrief_html is None:
            findings.append(
                self.finding(
                    severity,
                    "The study is flagged for deception but no debrief statement was loaded, "
                    "so raters would finish without being debriefed.",
                )
            )

        leftover = sorted(find_placeholders(terms.consent_markdown))
        if leftover:
            findings.append(
                self.finding(
                    severity,
                    "The rendered consent statement still contains unfilled placeholders: "
                    + ", ".join(f"{{{{{name}}}}}" for name in leftover),
                )
            )
        if experiment.name and experiment.name not in terms.consent_markdown:
            findings.append(
                self.finding(
                    softer,
                    f"The consent statement ({consent.ref}) does not mention the study name "
                    f"{experiment.name!r}; check it has a {{{{study_name}}}} placeholder.",
                )
            )
        length = format_session_length(resolve_session_policy(experiment).duration_minutes)
        if length not in terms.consent_markdown:
            findings.append(
                self.finding(
                    softer,
                    f"The consent statement ({consent.ref}) does not state the session length "
                    f"({length}); check it has a {{{{session_length}}}} placeholder.",
                )
            )
        details = (experiment.content_warning_details or "").strip()
        if needs is not None and needs.warned and details and details not in terms.consent_markdown:
            findings.append(
                self.finding(
                    softer,
                    f"The consent statement ({consent.ref}) does not include the content warning "
                    "details raters were promised; check it has a "
                    "{{content_warning_details}} placeholder.",
                )
            )
        if not findings:
            shown = f"Raters will be shown the {consent.ref} consent statement"
            if terms.debrief_html is not None:
                shown += " and a debrief at the end"
            findings.append(self.finding("info", shown + "."))
        return findings
