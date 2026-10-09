"""The synced team roster decides admin access; the env allowlist is break-glass."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config import get_settings
from main import create_app
from routers import admin as admin_router

SECRET = "sync-secret-for-tests"


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sync_secret: str = SECRET,
    allowlist: str = "",
) -> TestClient:
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "true")
    monkeypatch.setenv("ADMIN_ALLOWLIST", allowlist)
    monkeypatch.setenv("ACCESS_SYNC_SECRET", sync_secret)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    return TestClient(create_app())


def _sign_in_as(client: TestClient, email: str):
    async def fake_clerk_email_from_request() -> str:
        return email

    client.app.dependency_overrides[admin_router.get_clerk_email_from_request] = (
        fake_clerk_email_from_request
    )
    return client.post("/api/admin/auth/login", headers={"Authorization": "Bearer fake"})


def _push(client: TestClient, members: list[dict], secret: str = SECRET):
    return client.put(
        "/api/admin/access-roster",
        json={"members": members},
        headers={"Authorization": f"Bearer {secret}"},
    )


def test_push_is_disabled_until_a_secret_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    with _build_app(monkeypatch, sync_secret="") as client:
        response = _push(client, [{"email": "a@x.org", "role": "admin"}])
        assert response.status_code == 503


def test_push_rejects_wrong_secret_and_empty_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    with _build_app(monkeypatch) as client:
        wrong = _push(client, [{"email": "a@x.org", "role": "admin"}], secret="nope")
        assert wrong.status_code == 401

        empty = _push(client, [])
        assert empty.status_code == 400
        assert "empty" in empty.json()["detail"].lower()


def test_roster_grants_login_and_removal_takes_effect_on_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _build_app(monkeypatch) as client:
        pushed = _push(
            client,
            [
                {"email": "Owner@Sampura.org", "role": "admin"},
                {"email": "volunteer@gmail.com", "role": "member"},
            ],
        )
        assert pushed.status_code == 200
        assert pushed.json() == {"ok": True, "count": 2, "admins": 1}

        login = _sign_in_as(client, "volunteer@gmail.com")
        assert login.status_code == 200
        assert login.json() == {"ok": True, "role": "member"}
        assert client.get("/api/admin/experiments").status_code == 200

        # The group changed: the volunteer is gone. Their cookie is still
        # valid, but the per-request roster check now refuses them.
        assert _push(client, [{"email": "owner@sampura.org", "role": "admin"}]).status_code == 200
        denied = client.get("/api/admin/experiments")
        assert denied.status_code == 403
        assert denied.json()["detail"] == "Not in the team roster"

        relogin = _sign_in_as(client, "volunteer@gmail.com")
        assert relogin.status_code == 403
        assert relogin.json()["message"] == "Email is not in the team roster"


def test_duplicate_email_keeps_admin_and_listing_is_admin_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _build_app(monkeypatch) as client:
        assert client.get("/api/admin/access-roster").status_code == 403

        _push(
            client,
            [
                {"email": "both@x.org", "role": "member"},
                {"email": "both@x.org", "role": "admin"},
                {"email": "plain@x.org", "role": "member"},
            ],
        )
        assert _sign_in_as(client, "both@x.org").status_code == 200

        listing = client.get("/api/admin/access-roster")
        assert listing.status_code == 200
        rows = {row["email"]: row["role"] for row in listing.json()}
        assert rows == {"both@x.org": "admin", "plain@x.org": "member"}


def test_allowlist_is_break_glass_when_roster_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    with _build_app(monkeypatch, allowlist="rescue@x.org") as client:
        assert _sign_in_as(client, "rescue@x.org").status_code == 200
        assert client.get("/api/admin/experiments").status_code == 200
        assert _sign_in_as(client, "someone@x.org").status_code == 403
