from __future__ import annotations

import base64
import hmac
import json
import logging
import time
from datetime import datetime
from hashlib import sha256

from fastapi import HTTPException

from config import Settings
from session_policy import SessionPolicy

logger = logging.getLogger(__name__)


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


VERSION = "v1"


def issue_rater_session_token(
    settings: Settings,
    *,
    rater_id: int,
    experiment_id: int,
    session_start: datetime,
    policy: SessionPolicy,
) -> str:
    """Mint a token that dies exactly when the rater's session does.

    Derived from `session_start` rather than from `now`, because these are not
    the same instant: `start_session` re-mints on re-entry, and a TTL counted
    from the moment of minting handed a rater who re-entered near the deadline
    a token outliving their own session. The window that opened was the
    platform's only path for saving an in-flight answer, which is now an
    explicit grace period instead (issue #102).
    """
    now = int(time.time())
    exp = int(session_start.timestamp()) + policy.token_ttl_seconds
    payload = _b64url_json({"rid": rater_id, "eid": experiment_id, "iat": now, "exp": exp})
    sig = _sign(settings.effective_rater_session_secret, payload)
    return f"{VERSION}.{payload}.{sig}"


def verify_rater_session_token(settings: Settings, token: str) -> dict:
    try:
        ver, payload, sig = token.split(".")
    except ValueError:
        logger.warning("Rater session token malformed")
        raise HTTPException(status_code=401, detail="Invalid rater session")
    if ver != VERSION:
        logger.warning(
            "Rater session token version mismatch",
            extra={"attributes": {"got": ver, "expected": VERSION}},
        )
        raise HTTPException(status_code=401, detail="Invalid rater session")
    expected = _sign(settings.effective_rater_session_secret, payload)
    if not hmac.compare_digest(expected, sig):
        logger.warning("Rater session token signature invalid")
        raise HTTPException(status_code=401, detail="Invalid rater session")
    data = _unb64url_json(payload)
    rid = data.get("rid")
    eid = data.get("eid")
    iat = data.get("iat")
    exp = data.get("exp")
    try:
        rid = int(rid)
        eid = int(eid)
        iat = int(iat)
        exp = int(exp)
    except Exception:
        logger.warning("Rater session token payload invalid")
        raise HTTPException(status_code=401, detail="Invalid rater session")
    # Enforce TTL: expired tokens are treated as expired sessions
    now = int(time.time())
    if exp <= now:
        # Keep this aligned with frontend handling for expired sessions
        logger.warning(
            "Rater session token expired",
            extra={"attributes": {"rater_id": rid, "experiment_id": eid}},
        )
        raise HTTPException(status_code=403, detail="Session expired")
    logger.debug(
        "Rater session token verified",
        extra={"attributes": {"rater_id": rid, "experiment_id": eid}},
    )
    return {"rater_id": rid, "experiment_id": eid, "issued_at": iat, "expires_at": exp}
