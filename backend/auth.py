from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from database import get_session


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_json(obj: dict) -> str:
    return _b64url(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _unb64url(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _unb64url_json(data: str) -> dict:
    return json.loads(_unb64url(data))


def _sign(secret: str, payload: str) -> str:
    return _b64url(hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256).digest())


class AdminSession:
    def __init__(
        self,
        email: str,
        issued_at: int,
        expires_at: int | None = None,
        api_key_id: int | None = None,
    ):
        self.email = email
        self.issued_at = issued_at
        self.expires_at = expires_at
        # Set when the caller presented an admin-scoped API key instead of a
        # signed-in browser's cookie; `email` then names the key.
        self.api_key_id = api_key_id

    @property
    def via_api_key(self) -> bool:
        return self.api_key_id is not None


class AdminSessionManager:
    """Lightweight, stateless, signed session cookie for admin access.

    Cookie format: v1.<payload>.<signature>
      - payload: base64url({"email": str, "iat": int, "exp": int})
      - signature: base64url(HMAC_SHA256(secret, payload))
    """

    VERSION = "v1"

    def __init__(self, settings: Settings):
        self._settings = settings

    @property
    def cookie_name(self) -> str:
        return self._settings.hrp_session_cookie

    def _encode(self, email: str) -> str:
        now = int(time.time())
        exp = now + int(self._settings.hrp_session_max_age)
        payload = _b64url_json({"email": email, "iat": now, "exp": exp})
        sig = _sign(self._settings.app_secret_key, payload)
        return f"{self.VERSION}.{payload}.{sig}"

    def _decode(self, token: str) -> Optional[AdminSession]:
        try:
            ver, payload, sig = token.split(".")
        except ValueError:
            return None
        if ver != self.VERSION:
            return None
        expected = _sign(self._settings.app_secret_key, payload)
        if not hmac.compare_digest(expected, sig):
            return None
        data = _unb64url_json(payload)
        email = data.get("email")
        iat = data.get("iat")
        exp = data.get("exp")
        if not isinstance(email, str) or not email:
            return None
        try:
            iat = int(iat)
            exp = int(exp)
        except Exception:
            return None
        # Enforce server-side expiration regardless of browser cookie behavior
        now = int(time.time())
        if now > exp:
            return None
        return AdminSession(email=email, issued_at=iat, expires_at=exp)

    def set_cookie(self, response: Response, email: str) -> None:
        value = self._encode(email)
        # Use SameSite=None only when cookies are Secure (production, cross-site).
        # In local dev, sending a Secure cookie over http://localhost is rejected by browsers,
        # and SameSite=None without Secure is also rejected; use Lax for dev/proxy.
        same_site = "none" if self._settings.cookie_secure else "lax"
        response.set_cookie(
            key=self.cookie_name,
            value=value,
            max_age=self._settings.hrp_session_max_age,
            httponly=True,
            secure=self._settings.cookie_secure,
            samesite=same_site,
            path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(self.cookie_name, path="/")

    def get_session(self, request: Request) -> Optional[AdminSession]:
        token = request.cookies.get(self.cookie_name)
        if not token:
            return None
        return self._decode(token)


def get_admin_manager(settings: Settings = Depends(get_settings)) -> AdminSessionManager:
    return AdminSessionManager(settings)


def _extract_bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid Bearer token")
    return token


async def require_api_key(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_session),
) -> str:
    """Authenticate a programmatic client for the /api/v1 read API.

    Accepts ``Authorization: Bearer <key>`` and matches it against, in order:
      1. the DB-backed keys managed from the dashboard (looked up by prefix,
         constant-time hash compared), and
      2. the static ``api_keys`` env list — a fallback that keeps local dev and
         any pre-DB deployments working.

    Fails closed: a request with no matching key is always rejected. Returns a
    short principal label (the key's name/prefix, or ``env``) so callers can
    attribute usage without holding the secret.
    """
    # Deferred import: services.api_keys pulls in models, and auth.py is imported
    # early by main; importing at module load would risk a cycle.
    from services import api_keys as api_key_service

    token = _extract_bearer_token(request)

    # Static env keys first — a cheap, allocation-free check that also covers
    # the case where the DB has no keys yet.
    for key in settings.api_keys:
        if hmac.compare_digest(token, key):
            return "env"

    record = await api_key_service.verify_api_key(token, db)
    if record is not None:
        return record.name or record.prefix

    raise HTTPException(status_code=401, detail="Invalid API key")


def _allowlist(settings: Settings) -> set[str]:
    return {e.strip().lower() for e in settings.admin_allowlist}


async def _admin_session_from_api_key(
    request: Request, settings: Settings, db: AsyncSession
) -> AdminSession | None:
    """An admin session for a request bearing an admin-scoped API key.

    ``None`` when there is no bearer, or the bearer is not one of our keys
    (the dashboard's Clerk JWT, say), so the caller falls back to the cookie
    error the dashboard knows how to handle. A key that is ours but unknown,
    revoked or read-only is refused outright: the client meant to use it.

    The key acts for the admin who minted it, so it stops working when they
    leave the allowlist, as their own session would.
    """
    from services import api_keys as api_key_service

    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not api_key_service.is_api_key(token):
        return None

    record = await api_key_service.verify_api_key(token, db)
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if record.scope != "admin":
        raise HTTPException(
            status_code=403,
            detail="This API key is read-only (/api/v1); /api/admin needs an admin-scoped key",
        )
    if (record.created_by or "").strip().lower() not in _allowlist(settings):
        raise HTTPException(
            status_code=403,
            detail="This API key's creator is no longer allowlisted for admin access",
        )
    return AdminSession(email=f"api-key:{record.name}", issued_at=0, api_key_id=record.id)


async def require_admin(
    request: Request,
    settings: Settings = Depends(get_settings),
    manager: AdminSessionManager = Depends(get_admin_manager),
    db: AsyncSession = Depends(get_session),
) -> AdminSession:
    """Gate for /api/admin: a signed-in allowlisted browser, or an admin-scoped key.

    The cookie wins when both are sent. Static ``api_keys`` env entries are
    never admin: they have no scope and no audit row.
    """
    # Allow bypass in test/dev when explicitly disabled
    if not settings.admin_auth_enabled:
        return AdminSession(email="dev@local", issued_at=0)

    session = manager.get_session(request)
    if not session:
        key_session = await _admin_session_from_api_key(request, settings, db)
        if key_session is not None:
            return key_session
        raise HTTPException(status_code=403, detail="Admin session required")

    email = session.email.lower().strip()
    if not email or email not in _allowlist(settings):
        raise HTTPException(status_code=403, detail="Not allowlisted for admin access")

    return session


async def require_signed_in_admin(
    admin: AdminSession = Depends(require_admin),
) -> AdminSession:
    """`require_admin`, minus API keys: for what only a person should do.

    That is managing the keys themselves (a leaked key must not be able to
    mint, rotate or revive keys, or it could outlive its own revocation) and
    Prolific writes, as publishing spends money.
    """
    if admin.via_api_key:
        raise HTTPException(
            status_code=403,
            detail="This needs a signed-in dashboard session; an API key cannot do it",
        )
    return admin
