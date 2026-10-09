"""The team roster that decides who may use the admin surface.

The roster is the team Google Group. An Apps Script (ops/access-sync) reads
the group every few minutes and pushes the whole member list here, so the
table is always a copy of the group, never edited by hand. Group Owners and
Managers arrive as ``admin``, everyone else as ``member``.

Authorisation asks one question of this module: what role, if any, does an
email hold. The env ``ADMIN_ALLOWLIST`` is consulted only as break-glass, so
a broken sync cannot lock everyone out.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from models import AccessRole, AccessRosterEntry


class EmptyRosterError(ValueError):
    """Refused: applying an empty roster would remove everyone."""


def normalise(entries: list[tuple[str, AccessRole]]) -> dict[str, AccessRole]:
    """Lower-case, strip, de-duplicate. When one email appears twice, admin wins."""
    roster: dict[str, AccessRole] = {}
    for raw_email, role in entries:
        email = raw_email.strip().lower()
        if not email:
            continue
        if roster.get(email) == AccessRole.ADMIN:
            continue
        roster[email] = role
    return roster


async def replace_roster(entries: list[tuple[str, AccessRole]], db: AsyncSession) -> int:
    """Make the table equal to ``entries``, atomically. Returns the row count."""
    roster = normalise(entries)
    if not roster:
        raise EmptyRosterError("Refusing an empty roster")
    now = datetime.now(UTC)
    await db.execute(delete(AccessRosterEntry))
    # The column is a plain String, so store the enum's value, not the member.
    db.add_all(
        AccessRosterEntry(email=email, role=role.value, synced_at=now)
        for email, role in sorted(roster.items())
    )
    await db.commit()
    return len(roster)


async def list_roster(db: AsyncSession) -> list[AccessRosterEntry]:
    result = await db.execute(
        select(AccessRosterEntry).order_by(AccessRosterEntry.role, AccessRosterEntry.email)
    )
    return list(result.scalars().all())


async def role_for(email: str, settings: Settings, db: AsyncSession) -> AccessRole | None:
    """The role an email holds, or None if it has no access.

    The synced roster is the source of truth. The env allowlist is break-glass
    and grants admin regardless of the roster.
    """
    normalised = email.strip().lower()
    if not normalised:
        return None
    if normalised in {e.strip().lower() for e in settings.admin_allowlist}:
        return AccessRole.ADMIN
    entry = await db.get(AccessRosterEntry, normalised)
    # Loaded rows carry the raw string; hand callers the enum.
    return AccessRole(entry.role) if entry is not None else None
