"""Reading rater terms from their source: a bucket prefix or a local folder.

The source is the only place statements are edited. ``TERMS__SOURCE_URL`` is
``gs://bucket/prefix`` (a private bucket, read with the platform's own Google
credentials), ``https://`` (any plain web location), or ``file://`` (a local
folder, relative to the backend directory). Layout under it::

    manifest.json
    consent/<bundle>/v<N>.md
    debrief/<bundle>/v<N>.md

``manifest.json`` names the bundles, says which version of each file is
current, and declares which content-warning levels a bundle may serve::

    {
      "schema": 1,
      "bundles": {
        "standard":  {"label": "Standard", "content_warnings": ["none"], "consent": 1},
        "sensitive": {"label": "Sensitive content", "content_warnings": ["sensitive"],
                      "consent": 1, "debrief": 1}
      }
    }

Nothing here touches the database; see ``service`` for archiving and pinning.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from config import BASE_DIR, TermsSettings


from .render import KNOWN_PLACEHOLDERS, find_placeholders

MANIFEST_FILE = "manifest.json"
KIND_CONSENT = "consent"
KIND_DEBRIEF = "debrief"
_HTTP_TIMEOUT_SECONDS = 10.0
_BUNDLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Mirrors Prolific's content-warning levels plus "none"; the experiment-side enum
# arrives with the sensitive-content work and must stay in step with this.
_VALID_WARNINGS = {"none", "sensitive", "explicit"}


class TermsSourceError(Exception):
    """The source could not be read, or what it holds is invalid."""


@dataclass(frozen=True)
class BundleSpec:
    key: str
    label: str
    content_warnings: tuple[str, ...]
    consent_version: int
    debrief_version: int | None

    def permits(self, content_warning: str) -> bool:
        return content_warning in self.content_warnings

    def version_for(self, kind: str) -> int | None:
        return self.consent_version if kind == KIND_CONSENT else self.debrief_version


@dataclass(frozen=True)
class Manifest:
    bundles: dict[str, BundleSpec]

    def bundle(self, key: str) -> BundleSpec | None:
        return self.bundles.get(key)


def statement_path(bundle: str, kind: str, version: int) -> str:
    return f"{kind}/{bundle}/v{version}.md"


def _positive_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TermsSourceError(f"{where} must be a positive integer")
    return value


def parse_manifest(raw: str) -> Manifest:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TermsSourceError(f"{MANIFEST_FILE} is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise TermsSourceError(f"{MANIFEST_FILE} must be a JSON object")
    bundles_raw = data.get("bundles")
    if not isinstance(bundles_raw, dict) or not bundles_raw:
        raise TermsSourceError(f"{MANIFEST_FILE} needs a non-empty 'bundles' object")

    bundles: dict[str, BundleSpec] = {}
    for key, spec in bundles_raw.items():
        if not isinstance(key, str) or not _BUNDLE_KEY_RE.match(key):
            raise TermsSourceError(
                f"Bundle key {key!r} is invalid: lowercase letters, digits, '-' and '_' only"
            )
        if not isinstance(spec, dict):
            raise TermsSourceError(f"Bundle '{key}' must be a JSON object")
        warnings = spec.get("content_warnings")
        if (
            not isinstance(warnings, list)
            or not warnings
            or any(w not in _VALID_WARNINGS for w in warnings)
        ):
            raise TermsSourceError(
                f"Bundle '{key}' needs a non-empty 'content_warnings' list drawn from "
                f"{sorted(_VALID_WARNINGS)}"
            )
        consent_version = _positive_int(spec.get("consent"), f"Bundle '{key}' 'consent'")
        debrief_version = (
            _positive_int(spec.get("debrief"), f"Bundle '{key}' 'debrief'")
            if "debrief" in spec
            else None
        )
        serves_warning = any(w != "none" for w in warnings)
        if serves_warning and debrief_version is None:
            raise TermsSourceError(
                f"Bundle '{key}' serves a content warning, so it needs a 'debrief' version"
            )
        label = spec.get("label", key)
        if not isinstance(label, str) or not label.strip():
            raise TermsSourceError(f"Bundle '{key}' 'label' must be a non-empty string")
        bundles[key] = BundleSpec(
            key=key,
            label=label.strip(),
            content_warnings=tuple(dict.fromkeys(warnings)),
            consent_version=consent_version,
            debrief_version=debrief_version,
        )
    return Manifest(bundles=bundles)


def _file_root(settings: TermsSettings) -> Path:
    root = Path(settings.source_url[len("file://") :])
    return root if root.is_absolute() else BASE_DIR / root


_GCS_READ_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
_STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
_RENDER_TOKEN_FILE_ENV = "AWS_WEB_IDENTITY_TOKEN_FILE"


def wif_token_file(settings: TermsSettings) -> str:
    """Where the host leaves the OIDC identity token for federation."""
    return settings.gcs_wif_token_file or os.environ.get(_RENDER_TOKEN_FILE_ENV, "")


def wif_credentials(settings: TermsSettings, token_file: str):
    """Workload Identity Federation credentials: no key anywhere.

    The host's OIDC token (Render writes one per service and rotates it) is
    exchanged at Google's STS for a short-lived access token, optionally
    impersonating the platform's service account so the bucket grant lives on
    that one identity. Nothing here is a secret; it is all configuration.
    """
    from google.auth import identity_pool

    impersonation_url = None
    if settings.gcs_impersonate_service_account:
        impersonation_url = (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{settings.gcs_impersonate_service_account}:generateAccessToken"
        )
    return identity_pool.Credentials(
        audience=settings.gcs_wif_audience,
        subject_token_type="urn:ietf:params:oauth:token-type:jwt",
        token_url=_STS_TOKEN_URL,
        credential_source={"file": token_file},
        service_account_impersonation_url=impersonation_url,
        scopes=[_GCS_READ_SCOPE],
    )


@lru_cache(maxsize=1)
def _gcs_client():
    # Imported lazily so the dependency is only touched when a gs:// source
    # is configured. Two ways in, in order of preference:
    #   1. Workload Identity Federation (keyless), when TERMS__GCS_WIF_AUDIENCE
    #      is set and the host provides an OIDC token file.
    #   2. Application Default Credentials: a service-account key named by
    #      GOOGLE_APPLICATION_CREDENTIALS.
    import google.auth
    from google.cloud import storage

    from config import get_settings

    settings = get_settings().terms
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if settings.gcs_wif_audience:
        token_file = wif_token_file(settings)
        if not token_file:
            raise TermsSourceError(
                "TERMS__GCS_WIF_AUDIENCE is set but no identity token file was found: "
                f"set TERMS__GCS_WIF_TOKEN_FILE or let the host set {_RENDER_TOKEN_FILE_ENV}"
            )
        credentials = wif_credentials(settings, token_file)
    else:
        credentials, adc_project = google.auth.default()
        project = project or adc_project
    # The client insists on a project, but reading objects never uses it:
    # federated and user credentials carry none (and in a container there is
    # no gcloud binary to ask). Fall back to a placeholder rather than failing
    # a read that would succeed.
    return storage.Client(project=project or "rater-terms-reader", credentials=credentials)


def _download_gcs_text(bucket_name: str, blob_name: str) -> str:
    """Read one private object with the platform's own Google credentials."""
    from google.api_core import exceptions as gcs_exceptions
    from google.auth import exceptions as auth_exceptions

    uri = f"gs://{bucket_name}/{blob_name}"
    try:
        client = _gcs_client()
        return client.bucket(bucket_name).blob(blob_name).download_as_text(encoding="utf-8")
    except auth_exceptions.DefaultCredentialsError as exc:
        _gcs_client.cache_clear()
        raise TermsSourceError(
            "No Google credentials for the gs:// terms source: configure Workload Identity "
            "Federation (TERMS__GCS_WIF_AUDIENCE) or set GOOGLE_APPLICATION_CREDENTIALS"
        ) from exc
    except auth_exceptions.RefreshError as exc:
        _gcs_client.cache_clear()
        raise TermsSourceError(f"Google credentials for {uri} were refused: {exc}") from exc
    except gcs_exceptions.NotFound as exc:
        raise TermsSourceError(f"{uri} not found") from exc
    except gcs_exceptions.Forbidden as exc:
        raise TermsSourceError(
            f"No permission to read {uri}: grant the platform's service account "
            "roles/storage.objectViewer on the bucket"
        ) from exc
    except gcs_exceptions.GoogleAPIError as exc:
        raise TermsSourceError(f"Could not read {uri}: {exc}") from exc


async def fetch_text(settings: TermsSettings, relative_path: str) -> str:
    raw = settings.source_url.strip()
    if not raw:
        raise TermsSourceError(
            "TERMS__SOURCE_URL is not set. The platform ships no consent statements; "
            "point it at the team's terms source (gs://, https:// or file://)"
        )
    # Scheme checks use the untrimmed value: "gs://" alone would otherwise
    # lose its slashes and fall through to the "unknown scheme" error.
    base = raw.rstrip("/")
    if raw.startswith("file://"):
        path = _file_root(settings) / relative_path
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TermsSourceError(f"Could not read {path}: {exc.strerror or exc}") from exc
    if raw.startswith("gs://"):
        bucket_name, _, prefix = base[len("gs://") :].partition("/")
        if not bucket_name:
            raise TermsSourceError("TERMS__SOURCE_URL gs:// needs a bucket name")
        blob_name = f"{prefix}/{relative_path}" if prefix else relative_path
        # The storage client is synchronous; keep the event loop free.
        return await asyncio.to_thread(_download_gcs_text, bucket_name, blob_name)
    if raw.startswith(("http://", "https://")):
        url = f"{base}/{relative_path}"
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise TermsSourceError(f"Could not fetch {url}: {exc}") from exc
        if response.status_code != 200:
            raise TermsSourceError(f"Could not fetch {url}: HTTP {response.status_code}")
        return response.text
    raise TermsSourceError(
        "TERMS__SOURCE_URL must start with gs://, file://, http:// or https:// "
        f"(got {settings.source_url!r})"
    )


def source_url_for(settings: TermsSettings, relative_path: str) -> str:
    return f"{settings.source_url.rstrip('/')}/{relative_path}"


async def read_manifest(settings: TermsSettings) -> Manifest:
    return parse_manifest(await fetch_text(settings, MANIFEST_FILE))


async def read_statement(settings: TermsSettings, bundle: str, kind: str, version: int) -> str:
    path = statement_path(bundle, kind, version)
    text = await fetch_text(settings, path)
    if not text.strip():
        raise TermsSourceError(f"{path} is empty")
    unknown = find_placeholders(text) - KNOWN_PLACEHOLDERS
    if unknown:
        raise TermsSourceError(
            f"{path} uses unknown placeholder(s) {sorted(unknown)}; "
            f"allowed: {sorted(KNOWN_PLACEHOLDERS)}"
        )
    return text
