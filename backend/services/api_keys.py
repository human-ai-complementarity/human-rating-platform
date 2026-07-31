"""Issuance, storage, and verification of /api/v1 bearer keys.

Keys look like ``hrp_<43 url-safe random chars>``. Only the SHA-256 hash and a
non-secret ``prefix`` (``hrp_`` + 8 chars) are stored; the raw key is returned
to the caller exactly once. Verification looks a presented key up by prefix,
then constant-time compares its hash, so the stored secret never has to be
reconstructed.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ApiKey

KEY_SCHEME = "hrp_"
# `hrp_` + 8 chars. Long enough that prefix collisions are vanishingly rare, so
# the indexed lookup usually returns a single candidate.
PREFIX_LENGTH = len(KEY_SCHEME) + 8
# Only rewrite last_used_at when it's this stale, so a read endpoint doesn't
# incur a write on every single request.
LAST_USED_THROTTLE = timedelta(seconds=60)


@dataclass
class IssuedApiKey:
    """A freshly minted key: the persisted row plus the one-time plaintext."""

    record: ApiKey
    plaintext: str


def _generate_key() -> tuple[str, str, str]:
    """Return (plaintext, prefix, hash) for a new key."""
    plaintext = f"{KEY_SCHEME}{secrets.token_urlsafe(32)}"
    return plaintext, plaintext[:PREFIX_LENGTH], _hash_key(plaintext)


def _hash_key(plaintext: str) -> str:
    return sha256(plaintext.encode("utf-8")).hexdigest()


async def create_api_key(name: str, db: AsyncSession, *, created_by: str | None) -> IssuedApiKey:
    plaintext, prefix, key_hash = _generate_key()
    record = ApiKey(name=name.strip(), prefix=prefix, key_hash=key_hash, created_by=created_by)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return IssuedApiKey(record=record, plaintext=plaintext)


async def list_api_keys(db: AsyncSession) -> list[ApiKey]:
    """All keys, active and revoked, newest first (revoked kept for audit)."""
    result = await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc(), ApiKey.id.desc()))
    return list(result.scalars().all())


async def get_api_key_or_none(key_id: int, db: AsyncSession) -> ApiKey | None:
    return await db.get(ApiKey, key_id)


async def regenerate_api_key(record: ApiKey, db: AsyncSession) -> IssuedApiKey:
    """Rotate the secret in place: same row/name/id, new key, old one dead.

    Also clears any prior revocation so regenerating a revoked key reactivates
    it under a fresh secret.
    """
    plaintext, prefix, key_hash = _generate_key()
    record.prefix = prefix
    record.key_hash = key_hash
    record.revoked_at = None
    record.last_used_at = None
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return IssuedApiKey(record=record, plaintext=plaintext)


async def revoke_api_key(record: ApiKey, db: AsyncSession) -> ApiKey:
    if record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        db.add(record)
        await db.commit()
        await db.refresh(record)
    return record


async def verify_api_key(token: str, db: AsyncSession) -> ApiKey | None:
    """Return the matching active key for a presented token, or None.

    Looks up by prefix (indexed), then constant-time compares the hash against
    each non-revoked candidate. Best-effort stamps last_used_at on a hit.
    """
    prefix = token[:PREFIX_LENGTH]
    result = await db.execute(
        select(ApiKey).where(ApiKey.prefix == prefix).where(ApiKey.revoked_at.is_(None))
    )
    token_hash = _hash_key(token)
    for candidate in result.scalars().all():
        if hmac.compare_digest(token_hash, candidate.key_hash):
            await _touch_last_used(candidate, db)
            return candidate
    return None


async def _touch_last_used(record: ApiKey, db: AsyncSession) -> None:
    now = datetime.now(UTC)
    last = record.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if last is not None and now - last < LAST_USED_THROTTLE:
        return
    record.last_used_at = now
    db.add(record)
    await db.commit()


def mask(record: ApiKey) -> str:
    """Display form of a stored key: its prefix plus a masked tail."""
    return f"{record.prefix}{'•' * 6}"
