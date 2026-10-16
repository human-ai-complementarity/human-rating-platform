"""Reading rater terms from their source: a bucket prefix or a local folder.

The source is the only place statements are edited. ``TERMS__SOURCE_URL`` is
``gs://bucket/prefix`` (a private bucket, read with the platform's own Google
credentials), ``https://`` (any web location; plain ``http://`` is refused
because consent text must not be alterable in transit), or ``file://`` (a
local folder, relative to the backend directory). Layout under it::

    manifest.json
    consent/standard/v<N>.md
    consent/warned/v<N>.md
    debrief/v<N>.md

Exactly three documents exist: the consent statement for ordinary studies,
the consent statement for studies carrying a content warning, and the debrief
shown at the end of a study that needs one. ``manifest.json`` says which
version of each is current::

    {
      "documents": {
        "consent/standard": 1,
        "consent/warned": 1,
        "debrief": 1
      }
    }

Which document a study gets is decided by its flags (see ``service``), never
chosen by hand. Nothing here touches the database.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from config import BASE_DIR, TermsSettings

from .render import KNOWN_PLACEHOLDERS, find_placeholders

MANIFEST_FILE = "manifest.json"

# The documents a source must provide. The key doubles as the folder the
# versioned files live in.
CONSENT_STANDARD = "consent/standard"
CONSENT_WARNED = "consent/warned"
DEBRIEF = "debrief"
DOCUMENTS = (CONSENT_STANDARD, CONSENT_WARNED, DEBRIEF)

_HTTP_TIMEOUT_SECONDS = 10.0
# Tests swap in an httpx.MockTransport here; None means the real network.
_http_transport: httpx.AsyncBaseTransport | None = None


class TermsSourceError(Exception):
    """The source could not be read, or what it holds is invalid."""


@dataclass(frozen=True)
class Manifest:
    """The current version of each document."""

    versions: dict[str, int]

    def version_of(self, document: str) -> int:
        return self.versions[document]


def statement_path(document: str, version: int) -> str:
    return f"{document}/v{version}.md"


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
    documents = data.get("documents")
    if not isinstance(documents, dict):
        raise TermsSourceError(f"{MANIFEST_FILE} needs a 'documents' object")
    # Exactly the known set: a missing one would break a study that needs it,
    # and an unknown one is most likely a typo of a known one.
    missing = [d for d in DOCUMENTS if d not in documents]
    unknown = [d for d in documents if d not in DOCUMENTS]
    if missing or unknown:
        raise TermsSourceError(
            f"{MANIFEST_FILE} 'documents' must name exactly {list(DOCUMENTS)}"
            + (f"; missing {missing}" if missing else "")
            + (f"; unknown {unknown}" if unknown else "")
        )
    return Manifest(
        versions={
            document: _positive_int(documents[document], f"'{document}'") for document in DOCUMENTS
        }
    )


def _file_root(settings: TermsSettings) -> Path:
    root = Path(settings.source_url.strip()[len("file://") :])
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


@lru_cache(maxsize=4)
def _gcs_client(wif_audience: str, wif_token_file: str, impersonate: str):
    # Cached per credential configuration, so a caller's settings decide the
    # identity rather than the process-wide ones. Imported lazily so the
    # dependency is only touched when a gs:// source is configured. Two ways
    # in, in order of preference:
    #   1. Workload Identity Federation (keyless), when TERMS__GCS_WIF_AUDIENCE
    #      is set and the host provides an OIDC token file.
    #   2. Application Default Credentials: a service-account key named by
    #      GOOGLE_APPLICATION_CREDENTIALS.
    import google.auth
    from google.cloud import storage

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if wif_audience:
        if not wif_token_file:
            raise TermsSourceError(
                "TERMS__GCS_WIF_AUDIENCE is set but no identity token file was found: "
                f"set TERMS__GCS_WIF_TOKEN_FILE or let the host set {_RENDER_TOKEN_FILE_ENV}"
            )
        settings = TermsSettings(
            gcs_wif_audience=wif_audience,
            gcs_wif_token_file=wif_token_file,
            gcs_impersonate_service_account=impersonate,
        )
        credentials = wif_credentials(settings, wif_token_file)
    else:
        credentials, adc_project = google.auth.default()
        project = project or adc_project
    # The client insists on a project, but reading objects never uses it:
    # federated and user credentials carry none (and in a container there is
    # no gcloud binary to ask). Fall back to a placeholder rather than failing
    # a read that would succeed.
    return storage.Client(project=project or "rater-terms-reader", credentials=credentials)


def _download_gcs_text(settings: TermsSettings, bucket_name: str, blob_name: str) -> str:
    """Read one private object with the platform's own Google credentials."""
    from google.api_core import exceptions as gcs_exceptions
    from google.auth import exceptions as auth_exceptions

    uri = f"gs://{bucket_name}/{blob_name}"
    client_key = (
        settings.gcs_wif_audience,
        wif_token_file(settings),
        settings.gcs_impersonate_service_account,
    )
    try:
        client = _gcs_client(*client_key)
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
    # Anything else the read can raise: other API errors, auth transport
    # failures (google.auth wraps its own; requests' are OSErrors), a
    # connection reset mid-download, or a file that is not UTF-8. All of them
    # mean "the source is unavailable right now", which callers map to a 503,
    # never a 500.
    except (gcs_exceptions.GoogleAPIError, auth_exceptions.GoogleAuthError) as exc:
        raise TermsSourceError(f"Could not read {uri}: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise TermsSourceError(f"Could not read {uri}: {exc}") from exc


async def fetch_text(settings: TermsSettings, relative_path: str) -> str:
    raw = settings.source_url.strip()
    if not raw:
        raise TermsSourceError(
            "TERMS__SOURCE_URL is not set. The platform ships no consent statements; "
            "point it at the team's terms source (gs://, https:// or file://)"
        )
    base = raw.rstrip("/")
    if raw.startswith("file://"):
        path = _file_root(settings) / relative_path
        try:
            return path.read_text(encoding="utf-8")
        # ValueError covers UnicodeDecodeError: a non-UTF-8 file is unreadable
        # for our purposes just like a missing one.
        except (OSError, ValueError) as exc:
            detail = getattr(exc, "strerror", None) or exc
            raise TermsSourceError(f"Could not read {path}: {detail}") from exc
    if raw.startswith("gs://"):
        bucket_name, _, prefix = base[len("gs://") :].partition("/")
        if not bucket_name:
            raise TermsSourceError("TERMS__SOURCE_URL gs:// needs a bucket name")
        blob_name = f"{prefix}/{relative_path}" if prefix else relative_path
        # The storage client is synchronous; keep the event loop free.
        return await asyncio.to_thread(_download_gcs_text, settings, bucket_name, blob_name)
    if raw.startswith("https://"):
        url = f"{base}/{relative_path}"
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_SECONDS, transport=_http_transport
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise TermsSourceError(f"Could not fetch {url}: {exc}") from exc
        if response.status_code != 200:
            raise TermsSourceError(f"Could not fetch {url}: HTTP {response.status_code}")
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TermsSourceError(f"Could not read {url}: not UTF-8 text") from exc
    if raw.startswith("http://"):
        raise TermsSourceError(
            "TERMS__SOURCE_URL must use https://, not http://: consent text over a plain "
            "connection could be altered in transit"
        )
    raise TermsSourceError(
        f"TERMS__SOURCE_URL must start with gs://, file:// or https:// (got {settings.source_url!r})"
    )


def source_url_for(settings: TermsSettings, relative_path: str) -> str:
    return f"{settings.source_url.strip().rstrip('/')}/{relative_path}"


async def read_manifest(settings: TermsSettings) -> Manifest:
    return parse_manifest(await fetch_text(settings, MANIFEST_FILE))


async def read_statement(settings: TermsSettings, document: str, version: int) -> str:
    path = statement_path(document, version)
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
