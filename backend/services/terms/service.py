"""Rater terms: resolving, rendering, archiving and pinning statements.

Two moments read the source live: an admin opening an experiment's ethics
section (or previewing), and publishing a round. Publishing pins the current
versions by copying them into ``terms_statements``; from then on the
experiment's raters are served the archived copy and never depend on the
source being reachable. A preview rater of a still-unpinned experiment reads
live, and their consent archives the version it referenced.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import ContentWarning, Experiment, TermsStatement
from schemas import TermsBundleInfo, TermsPreviewResponse, TermsStatusResponse
from services.prolific_markdown import to_prolific_html
from session_policy import resolve_session_policy

from .render import format_session_length, render_markdown
from .source import (
    KIND_CONSENT,
    KIND_DEBRIEF,
    TermsSourceError,
    read_manifest,
    read_statement,
    source_url_for,
    statement_path,
)

logger = logging.getLogger(__name__)


def statement_ref(bundle: str, version: int) -> str:
    """How a pinned version is named to admins and in exports: "standard v2"."""
    return f"{bundle} v{version}"


@dataclass(frozen=True)
class StatementText:
    bundle: str
    kind: str
    version: int
    body: str
    source_url: str
    # Set when the text came from the archive; None when read live.
    statement_id: int | None = None

    @property
    def ref(self) -> str:
        return statement_ref(self.bundle, self.version)


@dataclass(frozen=True)
class RaterTerms:
    """What the rater endpoints need: rendered HTML plus the consent source."""

    consent: StatementText
    consent_markdown: str
    consent_html: str
    debrief_html: str | None


def _from_row(row: TermsStatement) -> StatementText:
    return StatementText(
        bundle=row.bundle,
        kind=row.kind,
        version=row.version,
        body=row.body_markdown,
        source_url=row.source_url,
        statement_id=row.id,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _serves_warning(experiment: Experiment) -> bool:
    return experiment.content_warning != ContentWarning.NONE.value


async def _read_live(experiment: Experiment) -> tuple[StatementText, StatementText | None]:
    settings = get_settings().terms
    manifest = await read_manifest(settings)
    spec = manifest.bundle(experiment.terms_bundle)
    if spec is None:
        raise TermsSourceError(
            f"Bundle '{experiment.terms_bundle}' is not in the terms manifest "
            f"(available: {', '.join(sorted(manifest.bundles))})"
        )
    if not spec.permits(experiment.content_warning):
        raise TermsSourceError(
            f"Bundle '{spec.key}' does not serve the '{experiment.content_warning}' "
            "content warning; pick a bundle that does"
        )

    async def load(kind: str, version: int) -> StatementText:
        body = await read_statement(settings, spec.key, kind, version)
        return StatementText(
            bundle=spec.key,
            kind=kind,
            version=version,
            body=body,
            source_url=source_url_for(settings, statement_path(spec.key, kind, version)),
        )

    consent = await load(KIND_CONSENT, spec.consent_version)
    debrief = None
    if _serves_warning(experiment):
        # parse_manifest guarantees a debrief version for any bundle that
        # serves a content warning.
        assert spec.debrief_version is not None
        debrief = await load(KIND_DEBRIEF, spec.debrief_version)
    return consent, debrief


async def resolve_terms(
    experiment: Experiment, db: AsyncSession
) -> tuple[StatementText, StatementText | None]:
    """Pinned experiments read the archive; unpinned ones read the source."""
    if experiment.consent_statement_id is None:
        return await _read_live(experiment)
    consent_row = await db.get(TermsStatement, experiment.consent_statement_id)
    if consent_row is None:
        raise TermsSourceError("Pinned consent statement is missing from the archive")
    debrief_row = (
        await db.get(TermsStatement, experiment.debrief_statement_id)
        if experiment.debrief_statement_id is not None
        else None
    )
    return _from_row(consent_row), (_from_row(debrief_row) if debrief_row else None)


async def archive_statement(statement: StatementText, db: AsyncSession) -> TermsStatement:
    """Copy a live statement into the archive, or return the existing copy.

    A version is immutable once archived: the same (bundle, kind, version)
    with different content is refused, so the fix is always a new version.
    Flushes but does not commit.
    """
    existing = (
        await db.execute(
            select(TermsStatement).where(
                TermsStatement.bundle == statement.bundle,
                TermsStatement.kind == statement.kind,
                TermsStatement.version == statement.version,
            )
        )
    ).scalar_one_or_none()
    digest = _sha256(statement.body)
    if existing is not None:
        if existing.sha256 != digest:
            raise TermsSourceError(
                f"{statement.bundle} {statement.kind} v{statement.version} has changed at the "
                f"source since it was archived; publish v{statement.version + 1} instead"
            )
        return existing
    row = TermsStatement(
        bundle=statement.bundle,
        kind=statement.kind,
        version=statement.version,
        body_markdown=statement.body,
        sha256=digest,
        source_url=statement.source_url,
        imported_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    logger.info(
        "Archived terms statement",
        extra={
            "attributes": {
                "bundle": row.bundle,
                "kind": row.kind,
                "version": row.version,
                "sha256": row.sha256,
            }
        },
    )
    return row


def _render(statement: StatementText, experiment: Experiment) -> str:
    policy = resolve_session_policy(experiment)
    return render_markdown(
        statement.body,
        study_name=experiment.name,
        session_length=format_session_length(policy.duration_minutes),
        content_warning_details=experiment.content_warning_details,
    )


def _unavailable(exc: TermsSourceError) -> HTTPException:
    return HTTPException(status_code=400, detail=f"Rater terms: {exc}")


async def terms_for_rater(experiment: Experiment, db: AsyncSession) -> RaterTerms:
    try:
        consent, debrief = await resolve_terms(experiment, db)
    except TermsSourceError as exc:
        logger.error(
            "Rater terms unavailable",
            extra={"attributes": {"experiment_id": experiment.id, "error": str(exc)}},
        )
        raise HTTPException(
            status_code=503,
            detail="This study's consent statement is unavailable right now. Please try again shortly.",
        ) from exc
    consent_markdown = _render(consent, experiment)
    return RaterTerms(
        consent=consent,
        consent_markdown=consent_markdown,
        consent_html=to_prolific_html(consent_markdown),
        debrief_html=to_prolific_html(_render(debrief, experiment)) if debrief else None,
    )


async def statement_for_consent(
    experiment: Experiment, db: AsyncSession
) -> tuple[TermsStatement, str]:
    """The archived statement row a consent record should point at, plus the
    rendered markdown the rater saw. Archives the live version for a still
    unpinned (preview) experiment. Flushes but does not commit."""
    terms = await terms_for_rater(experiment, db)
    if terms.consent.statement_id is not None:
        row = await db.get(TermsStatement, terms.consent.statement_id)
        assert row is not None
        return row, terms.consent_markdown
    try:
        row = await archive_statement(terms.consent, db)
    except TermsSourceError as exc:
        raise _unavailable(exc) from exc
    return row, terms.consent_markdown


def validate_terms_config(experiment: Experiment) -> None:
    """Field-level rules that need no source read."""
    if _serves_warning(experiment) and not (experiment.content_warning_details or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Content warning details are required when a content warning is set. "
                "Raters and Prolific both see them."
            ),
        )


async def check_bundle_choice(experiment: Experiment) -> None:
    """Refuse a bundle the source does not know or that cannot serve the
    experiment's content warning. Best effort on save: if the source cannot
    be read the save goes through, and publish (which must read it) is where
    the problem surfaces."""
    settings = get_settings().terms
    try:
        manifest = await read_manifest(settings)
    except TermsSourceError as exc:
        logger.warning(
            "Terms source unreadable while saving experiment; deferring bundle check to publish",
            extra={"attributes": {"experiment_id": experiment.id, "error": str(exc)}},
        )
        return
    spec = manifest.bundle(experiment.terms_bundle)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Terms bundle '{experiment.terms_bundle}' is not in the manifest "
                f"(available: {', '.join(sorted(manifest.bundles))})."
            ),
        )
    if not spec.permits(experiment.content_warning):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Terms bundle '{spec.key}' does not serve the "
                f"'{experiment.content_warning}' content warning."
            ),
        )


async def pin_terms(experiment: Experiment, db: AsyncSession) -> None:
    """Archive the current versions and record them on the experiment.

    Idempotent; a no-op once pinned. Reads the source live, so publishing is
    refused while it is unreachable or invalid. Flushes but does not commit.
    """
    if experiment.consent_statement_id is not None:
        return
    validate_terms_config(experiment)
    try:
        consent, debrief = await _read_live(experiment)
        experiment.consent_statement_id = (await archive_statement(consent, db)).id
        if debrief is not None:
            experiment.debrief_statement_id = (await archive_statement(debrief, db)).id
    except TermsSourceError as exc:
        raise _unavailable(exc) from exc
    await db.flush()
    logger.info(
        "Pinned rater terms",
        extra={
            "attributes": {
                "experiment_id": experiment.id,
                "consent": consent.ref,
                "debrief": debrief.ref if debrief else None,
            }
        },
    )


async def fetch_terms_refs(statement_ids: set[int], db: AsyncSession) -> dict[int, str]:
    """Map archived statement ids to their "bundle vN" labels."""
    ids = {i for i in statement_ids if i is not None}
    if not ids:
        return {}
    rows = (await db.execute(select(TermsStatement).where(TermsStatement.id.in_(ids)))).scalars()
    return {row.id: statement_ref(row.bundle, row.version) for row in rows}


async def get_terms_status() -> TermsStatusResponse:
    settings = get_settings().terms
    try:
        manifest = await read_manifest(settings)
    except TermsSourceError as exc:
        return TermsStatusResponse(source_url=settings.source_url, ok=False, error=str(exc))
    return TermsStatusResponse(
        source_url=settings.source_url,
        ok=True,
        bundles=[
            TermsBundleInfo(
                key=spec.key,
                label=spec.label,
                content_warnings=list(spec.content_warnings),
                consent_version=spec.consent_version,
                debrief_version=spec.debrief_version,
            )
            for spec in manifest.bundles.values()
        ],
    )


async def terms_preview(experiment: Experiment, db: AsyncSession) -> TermsPreviewResponse:
    """What this experiment's raters see, rendered with its placeholders."""
    try:
        consent, debrief = await resolve_terms(experiment, db)
    except TermsSourceError as exc:
        raise _unavailable(exc) from exc
    return TermsPreviewResponse(
        pinned=experiment.consent_statement_id is not None,
        consent_ref=consent.ref,
        consent_html=to_prolific_html(_render(consent, experiment)),
        debrief_ref=debrief.ref if debrief else None,
        debrief_html=to_prolific_html(_render(debrief, experiment)) if debrief else None,
    )
