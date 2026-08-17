"""End-to-end CRUD for datasets.

Uses the shared `client` fixture. Covers case-insensitive name uniqueness
(create and rename), wave-token normalization, partial PATCH semantics, and
deletion — the contract experiment groups will build on.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _create(client: TestClient, name: str, waves: list[str] | None = None) -> dict:
    payload: dict = {"name": name}
    if waves is not None:
        payload["waves"] = waves
    resp = client.post("/api/admin/datasets", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_and_list_ordered_by_name(client: TestClient) -> None:
    _create(client, "swe-bench-verified", ["fall25"])
    _create(client, "Argus")
    _create(client, "medqa", ["fall25", "sp26"])

    rows = client.get("/api/admin/datasets").json()
    assert [r["name"] for r in rows] == ["Argus", "medqa", "swe-bench-verified"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["medqa"]["waves"] == ["fall25", "sp26"]
    assert by_name["Argus"]["waves"] == []


def test_name_is_unique_case_insensitively(client: TestClient) -> None:
    created = _create(client, "SWE-bench")
    dup = client.post("/api/admin/datasets", json={"name": "swe-bench"})
    assert dup.status_code == 409
    # The stored (first-seen) casing is echoed in the error for discoverability.
    assert "SWE-bench" in dup.json()["detail"]

    rows = client.get("/api/admin/datasets").json()
    assert len(rows) == 1
    assert rows[0]["id"] == created["id"]


def test_name_is_trimmed_and_casing_preserved(client: TestClient) -> None:
    created = _create(client, "  SWE-bench Verified  ")
    assert created["name"] == "SWE-bench Verified"

    blank = client.post("/api/admin/datasets", json={"name": "   "})
    assert blank.status_code == 422


def test_waves_are_lowercased_and_deduped(client: TestClient) -> None:
    created = _create(client, "medqa", ["Fall25", "fall25", " SP26 ", "sp26"])
    assert created["waves"] == ["fall25", "sp26"]


def test_get_returns_single_dataset(client: TestClient) -> None:
    created = _create(client, "medqa", ["fall25"])
    fetched = client.get(f"/api/admin/datasets/{created['id']}").json()
    assert fetched == created

    assert client.get("/api/admin/datasets/9999").status_code == 404


def test_patch_is_partial(client: TestClient) -> None:
    created = _create(client, "medqa", ["fall25"])

    renamed = client.patch(f"/api/admin/datasets/{created['id']}", json={"name": "MedQA"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "MedQA"
    assert renamed.json()["waves"] == ["fall25"]  # untouched

    waved = client.patch(f"/api/admin/datasets/{created['id']}", json={"waves": ["fall25", "sp26"]})
    assert waved.json()["waves"] == ["fall25", "sp26"]
    assert waved.json()["name"] == "MedQA"  # untouched


def test_rename_to_taken_name_conflicts_but_recasing_self_is_fine(client: TestClient) -> None:
    a = _create(client, "medqa")
    _create(client, "swe-bench")

    conflict = client.patch(f"/api/admin/datasets/{a['id']}", json={"name": "SWE-BENCH"})
    assert conflict.status_code == 409

    # Changing only the casing of your own name is a no-conflict rename.
    recased = client.patch(f"/api/admin/datasets/{a['id']}", json={"name": "MedQA"})
    assert recased.status_code == 200, recased.text
    assert recased.json()["name"] == "MedQA"


def test_delete_removes_dataset(client: TestClient) -> None:
    created = _create(client, "medqa")
    resp = client.delete(f"/api/admin/datasets/{created['id']}")
    assert resp.status_code == 200

    assert client.get("/api/admin/datasets").json() == []
    assert client.delete(f"/api/admin/datasets/{created['id']}").status_code == 404
