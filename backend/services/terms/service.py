"""Rater terms: resolving, rendering, archiving and pinning statements.

Which documents a study uses follows from its flags (see ``flags``): the
standard or the warned consent statement, and for a deception study the
debrief. Two moments read the source live: an admin opening the consent
section (or previewing), and publishing a round. Publishing pins the current
versions by copying them into ``terms_statements``; from then on the
experiment's raters are served the archived copy and never depend on the
source being reachable. Before that, raters (previews) read live.

With no source configured at all, an unflagged study shows a placeholder
consent statement: the platform ships no statement text of its own, and a
deployment that has not set one up should see that on the consent screen
rather than a broken rater flow. A flagged study needs its real documents,
so it cannot be published without a source.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import Experiment, TermsStatement
from schemas import TermsPreviewResponse, TermsStatusResponse
from services.prolific_markdown import to_prolific_html
from session_policy import resolve_session_policy

from .flags import StudyRequirements, parse_flags, requirements_for
from .render import format_session_length, render_markdown
from .source import (
    DOCUMENTS,
    TermsSourceError,
    read_manifest,
    read_statement,
    source_url_for,
    statement_path,
)

logger = logging.getLogger(__name__)

# Shown when TERMS__SOURCE_URL is unset. Deliberately not a consent form:
# it tells whoever is looking that the deployment has no statement yet.
PLACEHOLDER_LABEL = "placeholder"
PLACEHOLDER_CONSENT = """\
## Consent statement not configured

This deployment of the Human Rating Platform has no consent statement set up \
yet, so none can be shown here. The study is **{{study_name}}** and you would \
have {{session_length}}.

If you are a participant, please return this study. If you run this \
deployment, point `TERMS__SOURCE_URL` at your organisation's consent \
statements; see the README.

By clicking **I agree** you acknowledge that no consent statement was shown.
"""


def statement_label(document: str) -> str:
    """How a document is named on consent records and in exports: the last
    path segment, so "consent/standard" is recorded as "standard"."""
    return document.rsplit("/", 1)[-1]


def statement_ref(label: str, version: int) -> str:
    """A version as shown to admins and in exports: "standard v2"."""
    return f"{label} v{version}"


@dataclass(frozen=True)
class StatementText:
    # "consent/standard", "consent/warned" or "debrief"; "" for the placeholder.
    document: str
    version: int
    body: str
    source_url: str
    # Set when the text came from the archive; None when read live.
    statement_id: int | None = None

    @property
    def label(self) -> str:
        return statement_label(self.document) if self.document else PLACEHOLDER_LABEL

    @property
    def ref(self) -> str:
        return statement_ref(self.label, self.version)

    @property
    def sha256(self) -> str:
        return _sha256(self.body)


@dataclass(frozen=True)
class RaterTerms:
    """What the rater endpoints need: rendered HTML plus the consent source."""

    consent: StatementText
    # The consent statement with placeholders filled: what the rater sees,
    # stored verbatim on their consent record.
    consent_markdown: str
    consent_html: str
    debrief_html: str | None


@dataclass(frozen=True)
class PendingPin:
    """The versions a first publish will archive, read and checked up front."""

    consent: StatementText
    debrief: StatementText | None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _from_row(row: TermsStatement) -> StatementText:
    return StatementText(
        document=row.document,
        version=row.version,
        body=row.body_markdown,
        source_url=row.source_url,
        statement_id=row.id,
    )


def _source_configured() -> bool:
    return bool(get_settings().terms.source_url.strip())


def _placeholder() -> StatementText:
    return StatementText(document="", version=0, body=PLACEHOLDER_CONSENT, source_url="")


def study_requirements(experiment: Experiment) -> StudyRequirements:
    return requirements_for(experiment.study_flags)


async def _read_live(experiment: Experiment) -> tuple[StatementText, StatementText | None]:
    settings = get_settings().terms
    manifest = await read_manifest(settings)
    needs = study_requirements(experiment)

    async def load(document: str) -> StatementText:
        version = manifest.version_of(document)
        body = await read_statement(settings, document, version)
        return StatementText(
            document=document,
            version=version,
            body=body,
            source_url=source_url_for(settings, statement_path(document, version)),
        )

    consent = await load(needs.consent_document)
    debrief = await load(needs.debrief_document) if needs.debrief_document else None
    return consent, debrief


async def resolve_terms(
    experiment: Experiment, db: AsyncSession
) -> tuple[StatementText, StatementText | None]:
    """Pinned experiments read the archive; unpinned ones read the source;
    with no source configured, the placeholder."""
    if experiment.consent_statement_id is not None:
        consent_row = await db.get(TermsStatement, experiment.consent_statement_id)
        if consent_row is None:
            raise TermsSourceError("Pinned consent statement is missing from the archive")
        debrief_row = (
            await db.get(TermsStatement, experiment.debrief_statement_id)
            if experiment.debrief_statement_id is not None
            else None
        )
        return _from_row(consent_row), (_from_row(debrief_row) if debrief_row else None)
    if not _source_configured():
        logger.warning(
            "TERMS__SOURCE_URL is unset; serving the placeholder consent statement",
            extra={"attributes": {"experiment_id": experiment.id}},
        )
        return _placeholder(), None
    return await _read_live(experiment)


async def _fetch_archived(statement: StatementText, db: AsyncSession) -> TermsStatement | None:
    """The archived row for this (document, version), if any, verified to hold
    the same text. A version is immutable once archived, so a mismatch means
    the file was edited in place and the fix is a new version."""
    existing = (
        await db.execute(
            select(TermsStatement).where(
                TermsStatement.document == statement.document,
                TermsStatement.version == statement.version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.sha256 != statement.sha256:
        raise TermsSourceError(
            f"{statement.document} v{statement.version} has changed at the source since it "
            f"was archived; publish v{statement.version + 1} instead"
        )
    return existing


async def archive_statement(statement: StatementText, db: AsyncSession) -> TermsStatement:
    """Copy a live statement into the archive, or return the existing copy.

    Flushes but does not commit. Two publishes archiving the same new version
    at once race on the unique constraint; the loser takes the winner's row.
    """
    existing = await _fetch_archived(statement, db)
    if existing is not None:
        return existing
    row = TermsStatement(
        document=statement.document,
        version=statement.version,
        body_markdown=statement.body,
        sha256=statement.sha256,
        source_url=statement.source_url,
        imported_at=datetime.now(UTC),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        existing = await _fetch_archived(statement, db)
        if existing is None:  # pragma: no cover - the constraint says it exists
            raise
        return existing
    logger.info(
        "Archived terms statement",
        extra={
            "attributes": {
                "document": row.document,
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


def validate_terms_config(experiment: Experiment) -> None:
    """Field-level rules that need no source read."""
    try:
        needs = study_requirements(experiment)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown study flag: {exc}") from exc
    if needs.warned and not (experiment.content_warning_details or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Content warning details are required for a sensitive or explicit study. "
                "Raters and Prolific both see them."
            ),
        )


async def prepare_terms_pin(experiment: Experiment, db: AsyncSession) -> PendingPin | None:
    """Read and check everything a first publish will pin, writing nothing.

    Runs before the Prolific calls so an unreadable source or a version
    edited in place refuses the publish up front. None when the experiment is
    already pinned, or when no source is configured and the study has no
    flags (raters then keep getting the placeholder). A flagged study needs
    its real documents, so without a source it cannot go live.
    """
    if experiment.consent_statement_id is not None:
        return None
    validate_terms_config(experiment)
    if not _source_configured():
        if study_requirements(experiment).flags:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Rater terms: this study has flags set, so it needs its warning consent "
                    "or debrief statement, but TERMS__SOURCE_URL is not set. Point it at the "
                    "team's terms source (a file:// folder works for development)."
                ),
            )
        logger.warning(
            "TERMS__SOURCE_URL is unset; publishing without pinned rater terms",
            extra={"attributes": {"experiment_id": experiment.id}},
        )
        return None
    try:
        consent, debrief = await _read_live(experiment)
        await _fetch_archived(consent, db)
        if debrief is not None:
            await _fetch_archived(debrief, db)
    except TermsSourceError as exc:
        raise _unavailable(exc) from exc
    return PendingPin(consent=consent, debrief=debrief)


async def apply_terms_pin(
    experiment: Experiment, pending: PendingPin | None, db: AsyncSession
) -> None:
    """Archive the prepared versions and record them on the experiment.

    Called once the study is live, in the same transaction that flips the
    experiment out of DRAFT, so a publish that fails at Prolific leaves
    nothing pinned and the terms still editable. Flushes but does not commit.

    `prepare_terms_pin` already gated the publish on these versions, so a
    failure here (a version edited in place between the two, under a
    concurrent publish) is logged rather than raised: raising would roll
    back the status flip while the study is already live on Prolific and
    strand the round. The experiment stays unpinned, serving the live text,
    and the next publish tries again.
    """
    if pending is None or experiment.consent_statement_id is not None:
        return
    try:
        consent_id = (await archive_statement(pending.consent, db)).id
        debrief_id = (await archive_statement(pending.debrief, db)).id if pending.debrief else None
    except TermsSourceError as exc:
        logger.error(
            "Rater terms could not be pinned after publish; the study is live unpinned",
            extra={"attributes": {"experiment_id": experiment.id, "error": str(exc)}},
        )
        return
    experiment.consent_statement_id = consent_id
    experiment.debrief_statement_id = debrief_id
    await db.flush()
    logger.info(
        "Pinned rater terms",
        extra={
            "attributes": {
                "experiment_id": experiment.id,
                "consent": pending.consent.ref,
                "debrief": pending.debrief.ref if pending.debrief else None,
            }
        },
    )


async def fetch_terms_refs(statement_ids: set[int | None], db: AsyncSession) -> dict[int, str]:
    """Map archived statement ids to their "standard v2" labels."""
    ids = {i for i in statement_ids if i is not None}
    if not ids:
        return {}
    rows = (await db.execute(select(TermsStatement).where(TermsStatement.id.in_(ids)))).scalars()
    return {row.id: statement_ref(statement_label(row.document), row.version) for row in rows}


async def get_terms_status() -> TermsStatusResponse:
    settings = get_settings().terms
    try:
        manifest = await read_manifest(settings)
    except TermsSourceError as exc:
        return TermsStatusResponse(source_url=settings.source_url, ok=False, error=str(exc))
    return TermsStatusResponse(
        source_url=settings.source_url,
        ok=True,
        documents={document: manifest.version_of(document) for document in DOCUMENTS},
    )


async def terms_preview(
    experiment: Experiment, db: AsyncSession, *, selection: dict[str, object] | None = None
) -> TermsPreviewResponse:
    """What this experiment's raters see, rendered with its placeholders.

    `selection` (study_flags, content_warning_details) previews settings the
    admin has chosen but not saved. It applies only while nothing is pinned:
    a published experiment shows its archived versions whatever the form says.
    """
    if selection and experiment.consent_statement_id is None:
        # A transient copy; never added to the session.
        experiment = Experiment(**{**experiment.model_dump(), **selection})
    try:
        parse_flags(experiment.study_flags)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown study flag: {exc}") from exc
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
