"""The Google Groups reader and how it feeds the admin allowlist."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from auth import allowed_emails
from config import Settings
from services import admin_groups
from services.admin_groups import API, read_group_emails

G = "team@example.com"
COHORT = "cohort@example.com"
SA = "platform@example.iam.gserviceaccount.com"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Routes keyed by (url, pageToken or lookup id); unknown routes 404."""

    def __init__(self, routes: dict[tuple[str, str], FakeResponse | dict]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))
        key = (url, params.get("groupKey.id") or params.get("pageToken") or "")
        hit = self.routes.get(key, FakeResponse(404, text="not found"))
        return hit if isinstance(hit, FakeResponse) else FakeResponse(200, hit)


def lookup(group: str) -> tuple[str, str]:
    return (f"{API}/groups:lookup", group)


def members(name: str, page: str = "") -> tuple[str, str]:
    return (f"{API}/{name}/memberships", page)


def person(email: str, kind: str = "USER") -> dict:
    return {"preferredMemberKey": {"id": email}, "type": kind, "roles": [{"name": "MEMBER"}]}


def test_nested_groups_are_walked_and_service_accounts_skipped() -> None:
    session = FakeSession(
        {
            lookup(G): {"name": "groups/top"},
            members("groups/top"): {
                "memberships": [
                    person("Owner@example.com"),
                    person(COHORT, "GROUP"),
                    person(SA, "SERVICE_ACCOUNT"),
                ]
            },
            lookup(COHORT): {"name": "groups/cohort"},
            members("groups/cohort"): {
                "memberships": [
                    person("vol@example.org"),
                    person("owner@example.com"),
                    person(G, "GROUP"),
                ]
            },
        }
    )
    assert read_group_emails([G], session) == {"owner@example.com", "vol@example.org"}
    # The cycle back to the top group is not re-read.
    assert sum(1 for url, _ in session.calls if url.endswith("groups:lookup")) == 2


def test_pagination_is_followed() -> None:
    session = FakeSession(
        {
            lookup(G): {"name": "groups/top"},
            members("groups/top"): {"memberships": [person("a@x.org")], "nextPageToken": "p2"},
            members("groups/top", "p2"): {"memberships": [person("b@x.org")]},
        }
    )
    assert read_group_emails([G], session) == {"a@x.org", "b@x.org"}


def test_unreadable_nested_group_is_skipped_with_a_warning(caplog) -> None:
    session = FakeSession(
        {
            lookup(G): {"name": "groups/top"},
            members("groups/top"): {
                "memberships": [person("a@x.org"), person(COHORT, "GROUP"), person("b@x.org")]
            },
            lookup(COHORT): FakeResponse(403, text="PERMISSION_DENIED"),
        }
    )
    with caplog.at_level(logging.WARNING, logger="services.admin_groups"):
        assert read_group_emails([G], session) == {"a@x.org", "b@x.org"}
    assert f"Skipping nested group {COHORT} under {G}" in caplog.text


def test_nested_group_failing_mid_way_contributes_nobody() -> None:
    session = FakeSession(
        {
            lookup(G): {"name": "groups/top"},
            members("groups/top"): {"memberships": [person("a@x.org"), person(COHORT, "GROUP")]},
            lookup(COHORT): {"name": "groups/cohort"},
            members("groups/cohort"): {"memberships": [person("c@x.org")], "nextPageToken": "p2"},
            members("groups/cohort", "p2"): FakeResponse(500, text="boom"),
        }
    )
    assert read_group_emails([G], session) == {"a@x.org"}


def test_unreadable_top_level_group_is_skipped_and_the_others_read(caplog) -> None:
    session = FakeSession(
        {
            lookup(G): FakeResponse(403, text="PERMISSION_DENIED"),
            lookup(COHORT): {"name": "groups/cohort"},
            members("groups/cohort"): {"memberships": [person("c@x.org")]},
        }
    )
    with caplog.at_level(logging.WARNING, logger="services.admin_groups"):
        assert read_group_emails([G, COHORT], session) == {"c@x.org"}
    assert f"Skipping group {G}: No permission to read {G}" in caplog.text
    assert "Groups Reader role" in caplog.text


def test_unknown_group_warns_pointing_at_the_setting(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="services.admin_groups"):
        assert read_group_emails(["nope@example.com"], FakeSession({})) == set()
    assert "nope@example.com not found (404): check ADMIN_GROUPS" in caplog.text


GOOD = {
    lookup(G): {"name": "groups/top"},
    members("groups/top"): {"memberships": [person("a@x.org")]},
}


@pytest.mark.asyncio
async def test_refresh_replaces_on_success_and_clears_after_repeated_failure(monkeypatch) -> None:
    monkeypatch.setattr(admin_groups, "_emails", frozenset())
    monkeypatch.setattr(admin_groups, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(admin_groups, "google_session", lambda: FakeSession(GOOD))
    await admin_groups.refresh([G])
    assert admin_groups.emails() == {"a@x.org"}

    attempts = 0

    def no_credentials() -> FakeSession:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("Could not automatically determine credentials")

    monkeypatch.setattr(admin_groups, "google_session", no_credentials)
    await admin_groups.refresh([G])
    assert attempts == 3
    assert admin_groups.emails() == set()


@pytest.mark.asyncio
async def test_refresh_survives_a_transient_failure(monkeypatch) -> None:
    monkeypatch.setattr(admin_groups, "_emails", frozenset({"a@x.org"}))
    monkeypatch.setattr(admin_groups, "RETRY_DELAYS", (0, 0))

    def flaky_then_good() -> FakeSession:
        monkeypatch.setattr(admin_groups, "google_session", lambda: FakeSession(GOOD))
        raise RuntimeError("token refresh failed")

    monkeypatch.setattr(admin_groups, "google_session", flaky_then_good)
    await admin_groups.refresh([G])
    assert admin_groups.emails() == {"a@x.org"}


def test_allowed_emails_is_the_union(monkeypatch) -> None:
    monkeypatch.setattr(admin_groups, "_emails", frozenset({"vol@example.org"}))
    settings = SimpleNamespace(admin_allowlist=[" Break-Glass@example.com "])
    assert allowed_emails(settings) == {"break-glass@example.com", "vol@example.org"}


def test_admin_groups_setting_parses_like_the_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.delenv("ADMIN_GROUPS", raising=False)
    assert Settings(_env_file=None).admin_groups == []
    monkeypatch.setenv("ADMIN_GROUPS", " a@example.com, b@example.com ")
    assert Settings(_env_file=None).admin_groups == ["a@example.com", "b@example.com"]
