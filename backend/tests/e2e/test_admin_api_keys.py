"""Admin-scoped API keys on the /api/admin gate, with admin auth enabled.

Keys are minted the only way they can be: from a signed-in session (a faked
Clerk login, as in test_admin_auth.py). The cookie is then dropped so every
later request authenticates with the bearer alone, as a CLI would.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from config import get_settings
from main import create_app
from routers import admin as admin_router

ADMIN_EMAIL = "admin@example.com"


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_ALLOWLIST", ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:

        async def fake_clerk_email_from_request() -> str:
            return ADMIN_EMAIL

        client.app.dependency_overrides[admin_router.get_clerk_email_from_request] = (
            fake_clerk_email_from_request
        )
        yield client


def _sign_in(client: TestClient) -> None:
    resp = client.post("/api/admin/auth/login", headers={"Authorization": "Bearer clerk-jwt"})
    assert resp.status_code == 200, resp.text


def _mint(client: TestClient, name: str, scope: str | None = None) -> dict:
    """Mint a key from a signed-in session, then sign out of the jar."""
    _sign_in(client)
    body = {"name": name} if scope is None else {"name": name, "scope": scope}
    resp = client.post("/api/admin/api-keys", json=body)
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    return resp.json()


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_keys_are_read_only_unless_minted_admin(admin_client: TestClient) -> None:
    assert _mint(admin_client, "reader")["scope"] == "read"
    assert _mint(admin_client, "writer", "admin")["scope"] == "admin"


def test_admin_key_drives_admin_routes(admin_client: TestClient) -> None:
    key = _mint(admin_client, "study-cli", "admin")["plaintext_key"]

    assert admin_client.get("/api/admin/experiments", headers=_bearer(key)).status_code == 200
    created = admin_client.post(
        "/api/admin/datasets", json={"name": "demo", "waves": ["fall26"]}, headers=_bearer(key)
    )
    assert created.status_code == 200, created.text
    # Still a /api/v1 key too.
    assert admin_client.get("/api/v1/experiments", headers=_bearer(key)).status_code == 200


def test_read_key_is_refused_on_admin_routes(admin_client: TestClient) -> None:
    key = _mint(admin_client, "reader")["plaintext_key"]

    resp = admin_client.get("/api/admin/experiments", headers=_bearer(key))
    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"]
    assert admin_client.get("/api/v1/experiments", headers=_bearer(key)).status_code == 200


def test_unknown_or_revoked_key_is_401(admin_client: TestClient) -> None:
    created = _mint(admin_client, "short-lived", "admin")
    _sign_in(admin_client)
    admin_client.post(f"/api/admin/api-keys/{created['id']}/revoke")
    admin_client.cookies.clear()

    for key in (created["plaintext_key"], "hrp_not-a-real-key"):
        resp = admin_client.get("/api/admin/experiments", headers=_bearer(key))
        assert resp.status_code == 401, key
        assert resp.json()["detail"] == "Invalid API key"


def test_a_non_key_bearer_still_gets_the_session_error(admin_client: TestClient) -> None:
    # The dashboard reacts to "Admin session required" by re-authenticating;
    # a stray Clerk JWT must not turn that into an API-key error.
    resp = admin_client.get("/api/admin/experiments", headers=_bearer("eyJhbGciOi.clerk.jwt"))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin session required"


def test_admin_key_cannot_manage_keys(admin_client: TestClient) -> None:
    created = _mint(admin_client, "study-cli", "admin")
    headers = _bearer(created["plaintext_key"])

    for method, path in (
        ("GET", "/api/admin/api-keys"),
        ("POST", f"/api/admin/api-keys/{created['id']}/regenerate"),
        ("POST", f"/api/admin/api-keys/{created['id']}/revoke"),
    ):
        resp = admin_client.request(method, path, headers=headers)
        assert resp.status_code == 403, path
        assert "signed-in dashboard session" in resp.json()["detail"]
    resp = admin_client.post(
        "/api/admin/api-keys", json={"name": "spawn", "scope": "admin"}, headers=headers
    )
    assert resp.status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/admin/experiments/1/prolific/pilot"),
        ("POST", "/api/admin/experiments/1/launch"),
        ("POST", "/api/admin/experiments/1/prolific/rounds"),
        ("PATCH", "/api/admin/experiments/1/prolific/rounds/1"),
        ("POST", "/api/admin/experiments/1/prolific/rounds/1/publish"),
        ("POST", "/api/admin/experiments/1/prolific/rounds/1/close"),
        ("DELETE", "/api/admin/experiments/1/prolific/rounds/1"),
    ],
)
def test_admin_key_cannot_touch_prolific(admin_client: TestClient, method: str, path: str) -> None:
    """Publishing spends money, so every Prolific write needs a signed-in admin.

    Refused before the route runs: no experiment or round 1 exists, and the
    answer is still the key refusal rather than a 404 or 422."""
    key = _mint(admin_client, "study-cli", "admin")["plaintext_key"]

    resp = admin_client.request(method, path, headers=_bearer(key), json={})
    assert resp.status_code == 403, path
    assert "signed-in dashboard session" in resp.json()["detail"]


def test_admin_key_still_reads_prolific_state(admin_client: TestClient) -> None:
    key = _mint(admin_client, "study-cli", "admin")["plaintext_key"]
    created = admin_client.post(
        "/api/admin/experiments", json={"name": "demo"}, headers=_bearer(key)
    )
    assert created.status_code == 200, created.text
    rounds = admin_client.get(
        f"/api/admin/experiments/{created.json()['id']}/prolific/rounds", headers=_bearer(key)
    )
    assert rounds.status_code == 200, rounds.text


def test_regenerate_keeps_the_scope(admin_client: TestClient) -> None:
    created = _mint(admin_client, "study-cli", "admin")
    _sign_in(admin_client)
    regen = admin_client.post(f"/api/admin/api-keys/{created['id']}/regenerate")
    admin_client.cookies.clear()

    assert regen.json()["scope"] == "admin"
    new_key = regen.json()["plaintext_key"]
    assert admin_client.get("/api/admin/experiments", headers=_bearer(new_key)).status_code == 200


def test_key_stops_when_its_creator_leaves_the_allowlist(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _mint(admin_client, "study-cli", "admin")["plaintext_key"]
    monkeypatch.setattr(get_settings(), "admin_allowlist", ["someone-else@example.com"])

    resp = admin_client.get("/api/admin/experiments", headers=_bearer(key))
    assert resp.status_code == 403
    assert "no longer allowlisted" in resp.json()["detail"]
