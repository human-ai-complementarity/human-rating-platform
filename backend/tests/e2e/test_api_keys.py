"""End-to-end lifecycle for DB-backed /api/v1 bearer keys.

Uses the shared `client` fixture (admin_auth_enabled=False, so the cookie-authed
admin management endpoints are open) and drives the full path: mint a key via
the admin API, use it against /api/v1, then regenerate and revoke it and confirm
the old secret stops working at each step.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _create_key(client: TestClient, name: str) -> dict:
    resp = client.post("/api/admin/api-keys", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _v1_status(client: TestClient, key: str) -> int:
    return client.get("/api/v1/experiments", headers={"Authorization": f"Bearer {key}"}).status_code


def test_created_key_authenticates_v1(client: TestClient) -> None:
    created = _create_key(client, "inference-pipeline")
    assert created["plaintext_key"].startswith("hrp_")
    assert created["is_active"] is True
    # masked_key never leaks the secret.
    assert created["plaintext_key"] not in created["masked_key"]

    assert _v1_status(client, created["plaintext_key"]) == 200
    assert _v1_status(client, "hrp_not-a-real-key") == 401


def test_list_hides_plaintext(client: TestClient) -> None:
    _create_key(client, "key-a")
    resp = client.get("/api/admin/api-keys")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "key-a"
    assert "plaintext_key" not in rows[0]
    assert rows[0]["masked_key"].startswith("hrp_")


def test_regenerate_rotates_secret(client: TestClient) -> None:
    created = _create_key(client, "rotate-me")
    old_key = created["plaintext_key"]
    assert _v1_status(client, old_key) == 200

    regen = client.post(f"/api/admin/api-keys/{created['id']}/regenerate")
    assert regen.status_code == 200, regen.text
    new_key = regen.json()["plaintext_key"]

    assert new_key != old_key
    # Old secret is dead, new one works — same key id/name throughout.
    assert _v1_status(client, old_key) == 401
    assert _v1_status(client, new_key) == 200
    assert regen.json()["id"] == created["id"]


def test_revoke_disables_key(client: TestClient) -> None:
    created = _create_key(client, "revoke-me")
    key = created["plaintext_key"]
    assert _v1_status(client, key) == 200

    revoke = client.post(f"/api/admin/api-keys/{created['id']}/revoke")
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["is_active"] is False

    assert _v1_status(client, key) == 401
    # The revoked row is kept (for audit), just inactive.
    rows = client.get("/api/admin/api-keys").json()
    assert len(rows) == 1
    assert rows[0]["is_active"] is False


def test_regenerate_reactivates_revoked_key(client: TestClient) -> None:
    created = _create_key(client, "resurrect")
    client.post(f"/api/admin/api-keys/{created['id']}/revoke")

    regen = client.post(f"/api/admin/api-keys/{created['id']}/regenerate")
    assert regen.status_code == 200
    assert regen.json()["is_active"] is True
    assert _v1_status(client, regen.json()["plaintext_key"]) == 200


def test_regenerate_missing_key_404(client: TestClient) -> None:
    assert client.post("/api/admin/api-keys/9999/regenerate").status_code == 404
    assert client.post("/api/admin/api-keys/9999/revoke").status_code == 404
