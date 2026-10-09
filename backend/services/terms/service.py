"""What a rater is shown and asked to agree to.

Reads the standard bundle's current consent statement live from the terms
source, renders the study's placeholders into it, and hands back everything
the consent record needs to say exactly what was shown. With no source
configured, a placeholder is shown instead: the platform ships no statement
text of its own, and a deployment that has not set one up should see that
on the consent screen rather than a broken rater flow.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from fastapi import HTTPException

from config import get_settings
from models import Experiment
from services.prolific_markdown import to_prolific_html
from session_policy import resolve_session_policy

from .render import format_session_length, render_markdown
from .source import (
    KIND_CONSENT,
    TermsSourceError,
    read_manifest,
    read_statement,
    source_url_for,
    statement_path,
)

logger = logging.getLogger(__name__)

# Until experiments can choose a bundle, every study uses this one.
DEFAULT_BUNDLE = "standard"

# Shown when TERMS__SOURCE_URL is unset. Deliberately not a consent form:
# it tells whoever is looking that the deployment has no statement yet.
PLACEHOLDER_BUNDLE = "placeholder"
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


@dataclass(frozen=True)
class RaterTerms:
    bundle: str
    version: int
    source_url: str
    # SHA-256 of the statement file as read, before placeholders.
    sha256: str
    # The statement with placeholders filled: what the rater sees, stored
    # verbatim on their consent record.
    consent_markdown: str
    consent_html: str


def _render(body: str, experiment: Experiment) -> str:
    return render_markdown(
        body,
        study_name=experiment.name,
        session_length=format_session_length(resolve_session_policy(experiment).duration_minutes),
        content_warning_details=None,
    )


async def terms_for_rater(experiment: Experiment) -> RaterTerms:
    """The consent statement this experiment's raters must agree to.

    Read live on every call: the source is the single place statements are
    edited, and a consent record keeps its own copy of the text shown.
    """
    settings = get_settings().terms
    if not settings.source_url.strip():
        logger.warning(
            "TERMS__SOURCE_URL is unset; serving the placeholder consent statement",
            extra={"attributes": {"experiment_id": experiment.id}},
        )
        rendered = _render(PLACEHOLDER_CONSENT, experiment)
        return RaterTerms(
            bundle=PLACEHOLDER_BUNDLE,
            version=0,
            source_url="",
            sha256=hashlib.sha256(PLACEHOLDER_CONSENT.encode("utf-8")).hexdigest(),
            consent_markdown=rendered,
            consent_html=to_prolific_html(rendered),
        )

    try:
        manifest = await read_manifest(settings)
        spec = manifest.bundle(DEFAULT_BUNDLE)
        if spec is None:
            raise TermsSourceError(
                f"Bundle '{DEFAULT_BUNDLE}' is not in the terms manifest "
                f"(available: {', '.join(sorted(manifest.bundles))})"
            )
        body = await read_statement(settings, spec.key, KIND_CONSENT, spec.consent_version)
    except TermsSourceError as exc:
        logger.error(
            "Rater terms unavailable",
            extra={"attributes": {"experiment_id": experiment.id, "error": str(exc)}},
        )
        raise HTTPException(
            status_code=503,
            detail="This study's consent statement is unavailable right now. Please try again shortly.",
        ) from exc

    rendered = _render(body, experiment)
    return RaterTerms(
        bundle=spec.key,
        version=spec.consent_version,
        source_url=source_url_for(
            settings, statement_path(spec.key, KIND_CONSENT, spec.consent_version)
        ),
        sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        consent_markdown=rendered,
        consent_html=to_prolific_html(rendered),
    )
