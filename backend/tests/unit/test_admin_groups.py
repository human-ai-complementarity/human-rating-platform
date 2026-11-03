"""The Google Groups reader and how it feeds the admin allowlist."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from auth import allowed_emails
from config import Settings
from services import admin_groups
from services.admin_groups import API, GroupReadError, read_group_emails

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


def test_unreadable_nested_group_fails_the_whole_read_naming_it() -> None:
    session = FakeSession(
        {
            lookup(G): {"name": "groups/top"},
            members("groups/top"): {"memberships": [person("a@x.org"), person(COHORT, "GROUP")]},
            lookup(COHORT): FakeResponse(403, text="PERMISSION_DENIED"),
        }
    )
    with pytest.raises(GroupReadError, match=f"read {COHORT}.*Member of that group"):
        read_group_emails([G], session)


def test_unknown_group_is_a_404_pointing_at_the_setting() -> None:
    with pytest.raises(GroupReadError, match="not found.*ADMIN_GROUPS"):
        read_group_emails(["nope@example.com"], FakeSession({}))


@pytest.mark.asyncio
async def test_refresh_replaces_on_success_and_keeps_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(admin_groups, "_emails", frozenset())
    good = FakeSession(
        {
            lookup(G): {"name": "groups/top"},
            members("groups/top"): {"memberships": [person("a@x.org")]},
        }
    )
    monkeypatch.setattr(admin_groups, "google_session", lambda: good)
    await admin_groups.refresh([G])
    assert admin_groups.emails() == {"a@x.org"}

    monkeypatch.setattr(admin_groups, "google_session", lambda: FakeSession({}))
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
