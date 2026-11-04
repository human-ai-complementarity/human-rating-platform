"""Admin access from Google Groups.

``ADMIN_GROUPS`` names Google Groups. Everyone in them, nested groups
included, is admitted to the admin surface exactly as if listed in
``ADMIN_ALLOWLIST``. The platform reads the groups through the Cloud Identity
Groups API as its own Google identity (the service-account key named by
``GOOGLE_APPLICATION_CREDENTIALS``) on boot and every few minutes after, and
keeps the emails in memory. A read that fails after retries clears the set, so
until the next good read only ``ADMIN_ALLOWLIST`` admits anyone.

The service account must be allowed to view the members of each group it
reads, nested ones included: a Member of each (external members allowed on
the group), or holding the Workspace "Groups Reader" role.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"
API = "https://cloudidentity.googleapis.com/v1"
PAGE_SIZE = 500
MAX_NESTING = 5
REFRESH_SECONDS = 300
RETRY_DELAYS = (5, 15)  # seconds to wait before the second and third attempt


class GroupReadError(RuntimeError):
    """The groups could not be read; the last good set stays in force."""


class Session(Protocol):
    """The slice of ``requests.Session`` the reader uses."""

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None): ...


_emails: frozenset[str] = frozenset()


def emails() -> frozenset[str]:
    """Everyone found in ``ADMIN_GROUPS`` at the last successful read."""
    return _emails


def read_group_emails(groups: list[str], session: Session) -> set[str]:
    """Walk each group, nested groups included, and return the people's emails."""
    found: set[str] = set()
    seen: set[str] = set()
    for group in groups:
        _walk(session, group.strip().lower(), found, seen, depth=0)
    return found


def _walk(session: Session, group: str, found: set[str], seen: set[str], depth: int) -> None:
    if not group or group in seen or depth > MAX_NESTING:
        return
    seen.add(group)
    name = _get(session, f"{API}/groups:lookup", {"groupKey.id": group}, group).get("name")
    if not name:
        raise GroupReadError(f"Group lookup for {group} returned no name")
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"view": "FULL", "pageSize": PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        payload = _get(session, f"{API}/{name}/memberships", params, group)
        for membership in payload.get("memberships", []):
            key = membership.get("preferredMemberKey") or {}
            email = str(key.get("id") or "").strip().lower()
            kind = str(membership.get("type") or "USER").upper()
            if not email:
                continue
            if kind == "GROUP":
                _walk(session, email, found, seen, depth + 1)
            elif kind != "SERVICE_ACCOUNT" and not email.endswith(".gserviceaccount.com"):
                # The platform's own identity is a member so it can read; it is not a person.
                found.add(email)
        page_token = payload.get("nextPageToken")
        if not page_token:
            return


def _get(session: Session, url: str, params: dict[str, Any], group: str) -> dict[str, Any]:
    try:
        response = session.get(url, params=params, timeout=30)
    except Exception as exc:
        raise GroupReadError(
            f"Cloud Identity API unreachable while reading {group}: {exc}"
        ) from exc
    if response.status_code == 403:
        raise GroupReadError(
            f"No permission to read {group} (403): the platform's service account must be a "
            "Member of that group or hold the Groups Reader role, and the Cloud Identity API "
            f"must be enabled on its project. {response.text[:300]}"
        )
    if response.status_code == 404:
        raise GroupReadError(f"Group {group} not found (404): check ADMIN_GROUPS")
    if response.status_code >= 400:
        raise GroupReadError(
            f"Reading {group} failed: {response.status_code} {response.text[:300]}"
        )
    return response.json()


def google_session() -> Session:
    """A session signed as the platform's Google identity.

    Application Default Credentials: the service-account key named by
    ``GOOGLE_APPLICATION_CREDENTIALS`` on Render, or a developer's gcloud
    login locally (whose quota project the session sends along).
    """
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _project = google.auth.default(scopes=[SCOPE])
    return AuthorizedSession(credentials)


async def refresh(groups: list[str]) -> None:
    """Replace the in-memory set from Google.

    A read is retried after each delay in ``RETRY_DELAYS`` so a blip changes
    nothing. If every attempt fails the set is cleared: only ``ADMIN_ALLOWLIST``
    admits anyone until a later refresh succeeds, so a broken sync can never
    keep admitting someone who has since been removed from a group.
    """
    global _emails
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            found = await asyncio.to_thread(lambda: read_group_emails(groups, google_session()))
        except Exception as exc:  # GroupReadError, or google-auth's own credential errors
            if delay is not None:
                logger.warning("Admin groups read failed (attempt %d), retrying: %s", attempt, exc)
                await asyncio.sleep(delay)
                continue
            logger.error(
                "Admin groups read failed %d times; admitting only ADMIN_ALLOWLIST until a "
                "read succeeds: %s",
                attempt,
                exc,
            )
            _emails = frozenset()
            return
        _emails = frozenset(found)
        logger.info("Admin groups: %d people across %s", len(found), ", ".join(groups))
        return


async def run_refresh_loop(groups: list[str]) -> None:
    while True:
        await refresh(groups)
        await asyncio.sleep(REFRESH_SECONDS)
