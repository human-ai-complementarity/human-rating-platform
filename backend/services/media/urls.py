"""Expiring bearer URLs for media the backend serves itself.

``/api/raters/media/<digest>?exp=<unix seconds>&sig=<hmac>``: the signature
covers the digest and the expiry under the rater session secret, so a URL is
as good as a GCS signed URL for its lifetime and no better. An ``<img>`` tag
cannot send the ``X-Rater-Session`` header, which is why the credential
travels in the URL. The path is relative; the frontend prefixes its API host.
"""

from __future__ import annotations

import hashlib
import hmac
import time

MEDIA_PATH = "/api/raters/media"


def _signature(secret: str, digest: str, expires: int) -> str:
    message = f"{digest}.{expires}".encode("ascii")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()[:32]


def proxy_url(secret: str, digest: str, *, ttl_seconds: int, now: float | None = None) -> str:
    expires = int((time.time() if now is None else now) + ttl_seconds)
    return f"{MEDIA_PATH}/{digest}?exp={expires}&sig={_signature(secret, digest, expires)}"


def verify_proxy_token(
    secret: str, digest: str, expires: int, signature: str, *, now: float | None = None
) -> bool:
    """True while the URL is within its lifetime and its signature matches."""
    if expires <= (time.time() if now is None else now):
        return False
    return hmac.compare_digest(_signature(secret, digest, expires), signature)
