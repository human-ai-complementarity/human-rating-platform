from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config import get_settings
from main import create_app
from routers import admin as admin_router


def _build_app_with_admin_env(
    monkeypatch: pytest.MonkeyPatch,
    admin_auth_enabled: bool = True,
    allowlist: str = "allowlisted@example.com",
) -> TestClient:
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_ALLOWLIST", allowlist)
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "true" if admin_auth_enabled else "false")
    # Rebuild settings with the new env
    get_settings.cache_clear()  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def test_no_session_cookie_returns_403_on_admin_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    with _build_app_with_admin_env(monkeypatch, admin_auth_enabled=True) as client:
        response = client.get("/api/admin/experiments")

        assert response.status_code == 403
        assert response.json()["detail"] == "Admin session required"


def test_missing_or_invalid_bearer_on_login_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    with _build_app_with_admin_env(monkeypatch, admin_auth_enabled=True) as client:
        # No Authorization header
        resp_missing = client.post("/api/admin/auth/login")
        assert resp_missing.status_code == 401
        assert resp_missing.json()["detail"] == "Missing Bearer token"

        # Authorization header with empty token
        resp_empty = client.post("/api/admin/auth/login", headers={"Authorization": "Bearer "})
        assert resp_empty.status_code == 401
        assert resp_empty.json()["detail"] == "Invalid Bearer token"


def test_allowlisted_login_sets_cookie_and_unlocks_admin_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _build_app_with_admin_env(
        monkeypatch,
        admin_auth_enabled=True,
        allowlist="admin@example.com",
    ) as client:

        async def fake_clerk_email_from_request() -> str:
            return "admin@example.com"

        client.app.dependency_overrides[admin_router.get_clerk_email_from_request] = (
            fake_clerk_email_from_request
        )

        response = client.post(
            "/api/admin/auth/login",
            headers={"Authorization": "Bearer fake-token"},
        )

        assert response.status_code == 200
        cookie_name = get_settings().hrp_session_cookie
        assert cookie_name in client.cookies

        # Cookie should authorize secure admin routes
        admin_resp = client.get("/api/admin/experiments")
        assert admin_resp.status_code == 200


def test_non_allowlisted_email_is_denied_and_cookie_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _build_app_with_admin_env(
        monkeypatch,
        admin_auth_enabled=True,
        allowlist="admin@example.com",
    ) as client:

        async def fake_clerk_email_from_request() -> str:
            return "other@example.com"

        client.app.dependency_overrides[admin_router.get_clerk_email_from_request] = (
            fake_clerk_email_from_request
        )

        response = client.post(
            "/api/admin/auth/login",
            headers={"Authorization": "Bearer fake-token"},
        )

        assert response.status_code == 403
        assert response.json()["message"] == "Email is not allowlisted"
        cookie_name = get_settings().hrp_session_cookie
        assert cookie_name not in client.cookies


def test_tampered_cookie_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with _build_app_with_admin_env(
        monkeypatch,
        admin_auth_enabled=True,
        allowlist="admin@example.com",
    ) as client:

        async def fake_clerk_email_from_request() -> str:
            return "admin@example.com"

        client.app.dependency_overrides[admin_router.get_clerk_email_from_request] = (
            fake_clerk_email_from_request
        )

        login_resp = client.post(
            "/api/admin/auth/login",
            headers={"Authorization": "Bearer fake-token"},
        )
        assert login_resp.status_code == 200

        cookie_name = get_settings().hrp_session_cookie
        original = login_resp.cookies.get(cookie_name)
        assert original

        # Replace the valid cookie in the client jar with a tampered value
        tampered = original[:-1] + ("x" if original[-1] != "x" else "y")
        client.cookies.clear()
        client.cookies.set(cookie_name, tampered)

        response = client.get("/api/admin/experiments")
        assert response.status_code == 403
        assert response.json()["detail"] == "Admin session required"
