"""Reading question media from its source, and handing raters a URL for it.

``MEDIA__SOURCE_URL`` is ``gs://bucket/prefix`` (a private bucket, read with
the platform's own Google credentials) or ``file://folder`` (local dev and
tests; relative to the backend directory). Under it, every object is named by
the SHA-256 hex digest of its bytes::

    <prefix>/
    ├── 76cac499903b2fb6f064...   (64 hex characters, no extension)
    └── f857fde42cfb9cdd1a2b...

This is the inference pipeline's media store layout
(``checkpoints/ingest_questions/media/``), so that directory syncs to the
bucket as is. A read re-hashes the bytes, so a replaced object is refused
rather than shown under the old reference.

Two deliveries to a rater's browser, both a bearer URL with at least
``MEDIA__URL_TTL_MINUTES`` of life left when handed out:

* a gs:// source with ``MEDIA__SIGNED_URLS`` on: a V4 signed URL, so GCS
  serves the bytes and the API never proxies them;
* otherwise: ``/api/raters/media/<digest>`` served by the backend, behind an
  HMAC token (``services/media/urls.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from config import BASE_DIR, MediaSettings
from services import gcs

from .blocks import IMAGE, Block
from .urls import proxy_url

logger = logging.getLogger(__name__)

# Existence checks and reads fan out to the bucket; keep the fan-out bounded.
_GCS_CONCURRENCY = 8
# Above this many digests one listing of the prefix beats one HEAD each.
_LIST_INSTEAD_OF_HEAD_ABOVE = 64

SIGNED_URL = "signed_url"
PROXY = "proxy"


class MediaSourceError(RuntimeError):
    """The media source is unset, misconfigured, or unavailable right now.

    A RuntimeError on purpose: that is the assistance methods' documented
    "give up" signal, so an unreadable image degrades a session to
    no-assistance the way a provider outage does.
    """


class MediaNotFound(MediaSourceError):
    """No object under the digest, or the object no longer hashes to it."""


def configured(settings: MediaSettings) -> bool:
    return bool(settings.source_url.strip())


def _source(settings: MediaSettings) -> str:
    raw = settings.source_url.strip()
    if not raw:
        raise MediaSourceError(
            "MEDIA__SOURCE_URL is not set: point it at the bucket or folder holding the "
            "images this dataset references (gs://bucket/prefix or file://folder)"
        )
    if raw.startswith("gs://") or raw.startswith("file://"):
        return raw.rstrip("/")
    raise MediaSourceError(
        f"MEDIA__SOURCE_URL must start with gs:// or file:// (got {settings.source_url!r})"
    )


def delivery(settings: MediaSettings) -> str:
    """How a rater gets an image: ``SIGNED_URL`` or ``PROXY``."""
    source = _source(settings)
    return SIGNED_URL if source.startswith("gs://") and settings.signed_urls else PROXY


def _file_root(source: str) -> Path:
    root = Path(source[len("file://") :])
    return root if root.is_absolute() else BASE_DIR / root


def _bucket(source: str) -> tuple[str, str]:
    try:
        return gcs.split_gs_url(source, setting="MEDIA__SOURCE_URL")
    except gcs.GcsAccessError as exc:
        raise MediaSourceError(str(exc)) from exc


def _scopes(settings: MediaSettings) -> gcs.Scopes:
    # Signing through IAM signBlob needs the iam scope; a key signs locally
    # and ignores the scopes. Harmless to ask for it either way.
    if settings.signed_urls:
        return (gcs.GCS_READ_SCOPE, gcs.IAM_SCOPE)
    return gcs.READ_SCOPES


def _client(settings: MediaSettings):
    try:
        return gcs.gcs_client(*gcs.access_key(settings), _scopes(settings), setting_prefix="MEDIA")
    except gcs.GcsAccessError as exc:
        raise MediaSourceError(str(exc)) from exc


def _credentials(settings: MediaSettings):
    try:
        credentials, _ = gcs.gcs_credentials(
            *gcs.access_key(settings), _scopes(settings), setting_prefix="MEDIA"
        )
    except gcs.GcsAccessError as exc:
        raise MediaSourceError(str(exc)) from exc
    return credentials


@contextmanager
def _translating_gcs_errors(uri: str) -> Iterator[None]:
    """Map the storage client's failures onto ``MediaSourceError``.

    Everything the client can raise means "the source is unavailable right
    now", which callers turn into a 503, never a 500; a missing object is
    ``MediaNotFound``.
    """
    from google.api_core import exceptions as gcs_exceptions
    from google.auth import exceptions as auth_exceptions

    try:
        yield
    except MediaSourceError:
        raise
    except auth_exceptions.DefaultCredentialsError as exc:
        gcs.clear_cached_clients()
        raise MediaSourceError(
            "No Google credentials for the gs:// media source: configure Workload Identity "
            "Federation (MEDIA__GCS_WIF_AUDIENCE) or set GOOGLE_APPLICATION_CREDENTIALS"
        ) from exc
    except auth_exceptions.RefreshError as exc:
        gcs.clear_cached_clients()
        raise MediaSourceError(f"Google credentials for {uri} were refused: {exc}") from exc
    except gcs_exceptions.NotFound as exc:
        raise MediaNotFound(f"{uri} not found") from exc
    except gcs_exceptions.Forbidden as exc:
        raise MediaSourceError(
            f"No permission to read {uri}: grant the platform's service account "
            "roles/storage.objectViewer on the bucket"
        ) from exc
    except (
        gcs_exceptions.GoogleAPIError,
        auth_exceptions.GoogleAuthError,
        OSError,
        ValueError,
    ) as exc:
        raise MediaSourceError(f"Could not read {uri}: {exc}") from exc


# The pipeline's `sniff_mime`, so both sides read the same bytes the same way.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def sniff_mime(data: bytes) -> str:
    """The image type by its first bytes; the stored mime_type is not trusted
    to describe the object, the object is."""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _verify(digest: str, data: bytes, where: str) -> bytes:
    if hashlib.sha256(data).hexdigest() != digest:
        raise MediaNotFound(f"{where} no longer hashes to {digest}: it changed after upload")
    return data


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _read_file(root: Path, digest: str) -> bytes:
    path = root / digest
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise MediaNotFound(f"{path} not found") from None
    except OSError as exc:
        raise MediaSourceError(f"Could not read {path}: {exc.strerror or exc}") from exc
    return _verify(digest, data, str(path))


def _read_gcs(settings: MediaSettings, bucket_name: str, prefix: str, digest: str) -> bytes:
    blob_name = gcs.blob_name_under(prefix, digest)
    uri = f"gs://{bucket_name}/{blob_name}"
    with _translating_gcs_errors(uri):
        data = _client(settings).bucket(bucket_name).blob(blob_name).download_as_bytes()
    return _verify(digest, data, uri)


async def read_bytes(settings: MediaSettings, digest: str) -> bytes:
    """The bytes behind ``digest``, re-hashed against it."""
    source = _source(settings)
    if source.startswith("file://"):
        return await asyncio.to_thread(_read_file, _file_root(source), digest)
    bucket_name, prefix = _bucket(source)
    # The storage client is synchronous; keep the event loop free.
    return await asyncio.to_thread(_read_gcs, settings, bucket_name, prefix, digest)


def _exists_gcs(settings: MediaSettings, bucket_name: str, prefix: str, digest: str) -> bool:
    blob_name = gcs.blob_name_under(prefix, digest)
    with _translating_gcs_errors(f"gs://{bucket_name}/{blob_name}"):
        return bool(_client(settings).bucket(bucket_name).blob(blob_name).exists())


def _list_gcs(settings: MediaSettings, bucket_name: str, prefix: str) -> set[str]:
    """The digests the prefix holds: every direct child named like one."""
    folder = f"{prefix}/" if prefix else ""
    with _translating_gcs_errors(f"gs://{bucket_name}/{folder}"):
        blobs = _client(settings).list_blobs(bucket_name, prefix=folder)
        return {name for blob in blobs if (name := blob.name[len(folder) :]) and "/" not in name}


async def missing_digests(settings: MediaSettings, digests: list[str]) -> list[str]:
    """The digests the source holds no object for, in the order given.

    An upload calls this before storing rows, so a dataset whose images were
    never synced is refused at upload rather than shown broken to raters. A
    handful of digests are HEADed one by one; a dataset's worth is checked
    against one listing of the prefix instead.
    """
    if not digests:
        return []
    source = _source(settings)
    if source.startswith("file://"):
        root = _file_root(source)
        return [d for d in digests if not (root / d).is_file()]
    bucket_name, prefix = _bucket(source)
    if len(digests) > _LIST_INSTEAD_OF_HEAD_ABOVE:
        held = await asyncio.to_thread(_list_gcs, settings, bucket_name, prefix)
        return [d for d in digests if d not in held]
    limit = asyncio.Semaphore(_GCS_CONCURRENCY)

    async def check(digest: str) -> bool:
        async with limit:
            return await asyncio.to_thread(_exists_gcs, settings, bucket_name, prefix, digest)

    present = await asyncio.gather(*(check(d) for d in digests))
    return [d for d, ok in zip(digests, present) if not ok]


# ---------------------------------------------------------------------------
# URLs for raters
# ---------------------------------------------------------------------------


@dataclass
class _SignedUrl:
    url: str
    expires_at: float


# A URL is minted for twice the TTL and reused until only the TTL is left,
# so every URL handed out has at least the TTL of life, and each image is
# signed once per TTL however many raters reach it: through IAM signBlob a
# mint is an API call.
_signed_cache: dict[str, _SignedUrl] = {}
_SIGNED_CACHE_LIMIT = 4096


def _sign_gcs(
    settings: MediaSettings, bucket_name: str, prefix: str, digest: str, mime_type: str
) -> str:
    blob_name = gcs.blob_name_under(prefix, digest)
    uri = f"gs://{bucket_name}/{blob_name}"
    with _translating_gcs_errors(uri):
        blob = _client(settings).bucket(bucket_name).blob(blob_name)
        credentials = _credentials(settings)
        kwargs: dict = {}
        if not hasattr(credentials, "sign_bytes"):
            # Federated credentials carry no private key: the storage client
            # signs through the IAM signBlob API as the named service
            # account, with the access token it already holds. That account
            # needs roles/iam.serviceAccountTokenCreator on itself.
            if not settings.gcs_impersonate_service_account:
                raise MediaSourceError(
                    "Signed media URLs need a signer: set "
                    "MEDIA__GCS_IMPERSONATE_SERVICE_ACCOUNT (signing goes through IAM "
                    "signBlob as that account), use a service-account key, or set "
                    "MEDIA__SIGNED_URLS=false to serve images through the API"
                )
            if not credentials.valid:
                import google.auth.transport.requests

                credentials.refresh(google.auth.transport.requests.Request())
            kwargs = {
                "service_account_email": settings.gcs_impersonate_service_account,
                "access_token": credentials.token,
            }
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=2 * settings.url_ttl_minutes),
            method="GET",
            response_type=mime_type,
            **kwargs,
        )


async def _signed_url(settings: MediaSettings, digest: str, mime_type: str) -> str:
    now = time.time()
    ttl = settings.url_ttl_minutes * 60
    cached = _signed_cache.get(digest)
    if cached is not None and cached.expires_at - now >= ttl:
        return cached.url
    bucket_name, prefix = _bucket(_source(settings))
    url = await asyncio.to_thread(_sign_gcs, settings, bucket_name, prefix, digest, mime_type)
    if len(_signed_cache) >= _SIGNED_CACHE_LIMIT:
        _signed_cache.clear()
    _signed_cache[digest] = _SignedUrl(url, now + 2 * ttl)
    return url


async def url_for(settings: MediaSettings, secret: str, digest: str, mime_type: str) -> str:
    """A URL a rater's browser can fetch ``digest`` from for at least the TTL."""
    if delivery(settings) == SIGNED_URL:
        return await _signed_url(settings, digest, mime_type)
    return proxy_url(secret, digest, ttl_seconds=settings.url_ttl_minutes * 60)


async def render_blocks(
    settings: MediaSettings, secret: str, blocks: list[Block] | None
) -> list[Block] | None:
    """The blocks a rater is sent: text as is, each image by a fetchable URL.

    The content address is not sent: the URL is all the browser needs, and a
    signed URL carries the object name anyway.
    """
    if not blocks:
        return None
    rendered: list[Block] = []
    for block in blocks:
        if block["type"] != IMAGE:
            rendered.append(block)
            continue
        digest = block["ref"].removeprefix("sha256:")
        url = await url_for(settings, secret, digest, block["mime_type"])
        rendered.append({"type": IMAGE, "url": url, "mime_type": block["mime_type"]})
    return rendered
