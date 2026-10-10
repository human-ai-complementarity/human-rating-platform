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


def test_card_fields_round_trip_and_drive_launch_readiness(client: TestClient) -> None:
    """A hand-created dataset can be made launchable, then complete, without a deploy.

    This is why the card lives in columns rather than in a file shipped with
    the code: the vendored roster in `dataset_catalog.py` carries names and
    waves only, and a `POST /admin/datasets` row is not in it at all, so a
    read-through design would leave such rows permanently un-launchable.
    """
    created = _create(client, "hand-made", ["fall25"])
    assert created["launch_ready"] is False
    assert created["complete"] is False
    assert "study_blurb" in created["missing_for_launch"]
    assert "reward" not in created["missing_for_launch"]
    assert "reward" in created["missing_for_complete"]
    assert created["study_blurb"] is None

    filled = client.patch(
        f"/api/admin/datasets/{created['id']}",
        json={
            "external_study_name": "Passage rating",
            "internal_study_name": "hand-made fall25",
            "study_blurb": "Rate short passages.",
        },
    )
    assert filled.status_code == 200, filled.text
    body = filled.json()
    assert body["launch_ready"] is True
    assert body["missing_for_launch"] == []
    # Economics are optional at onboarding, but the card isn't complete without them.
    assert body["complete"] is False
    assert body["missing_for_complete"] == ["estimated_completion_time", "reward"]
    assert body["name"] == "hand-made"  # untouched by a card-only PATCH

    completed = client.patch(
        f"/api/admin/datasets/{created['id']}",
        json={"estimated_completion_time": 20, "reward": 450},
    ).json()
    assert completed["complete"] is True
    assert completed["missing_for_complete"] == []
    assert client.get(f"/api/admin/datasets/{created['id']}").json()["complete"] is True


def test_declared_empty_list_differs_from_undeclared(client: TestClient) -> None:
    """`None` is "nobody has said"; `[]` is a declaration of "none"."""
    created = _create(client, "screened")
    assert client.get(f"/api/admin/datasets/{created['id']}").json()["screeners"] is None

    client.patch(f"/api/admin/datasets/{created['id']}", json={"screeners": []})
    assert client.get(f"/api/admin/datasets/{created['id']}").json()["screeners"] == []

    client.patch(f"/api/admin/datasets/{created['id']}", json={"screeners": ["ai_taskers"]})
    assert client.get(f"/api/admin/datasets/{created['id']}").json()["screeners"] == ["ai_taskers"]


def test_card_fields_are_accepted_at_create(client: TestClient) -> None:
    resp = client.post(
        "/api/admin/datasets",
        json={"name": "at-create", "waves": ["sp26"], "study_blurb": "A blurb."},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["study_blurb"] == "A blurb."


def test_omitted_card_field_is_unchanged_while_explicit_null_clears(client: TestClient) -> None:
    created = _create(client, "partial")
    client.patch(f"/api/admin/datasets/{created['id']}", json={"study_blurb": "A blurb."})

    client.patch(f"/api/admin/datasets/{created['id']}", json={"study_label": "annotation"})
    assert client.get(f"/api/admin/datasets/{created['id']}").json()["study_blurb"] == "A blurb."

    client.patch(f"/api/admin/datasets/{created['id']}", json={"study_blurb": None})
    assert client.get(f"/api/admin/datasets/{created['id']}").json()["study_blurb"] is None


def test_bad_name_templates_are_refused_when_the_card_is_saved(client: TestClient) -> None:
    """The render rules apply on save, not first at experiment create."""
    resp = client.post(
        "/api/admin/datasets",
        json={"name": "templated", "internal_study_name": "{dataset} {waves}"},
    )
    assert resp.status_code == 400
    assert "internal_study_name" in resp.json()["detail"]
    assert client.get("/api/admin/datasets").json() == []

    created = _create(client, "templated")
    for body, field in (
        # The public name may only use {dataset}: raters read it.
        ({"external_study_name": "{dataset} {method}"}, "external_study_name"),
        ({"internal_study_name": "{dataset} - Round 1"}, "internal_study_name"),
    ):
        resp = client.patch(f"/api/admin/datasets/{created['id']}", json=body)
        assert resp.status_code == 400, body
        assert field in resp.json()["detail"]
    stored = client.get(f"/api/admin/datasets/{created['id']}").json()
    assert stored["external_study_name"] is None
    assert stored["internal_study_name"] is None

    ok = client.patch(
        f"/api/admin/datasets/{created['id']}",
        json={"external_study_name": "{dataset}", "internal_study_name": "{dataset} {wave}"},
    )
    assert ok.status_code == 200, ok.text
