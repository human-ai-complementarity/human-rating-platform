"""Tag behavior: creation-flow attachment, normalization, usage-ranked
suggestions, list filtering, and lifecycle (edit/duplicate/archive/delete).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _create_experiment(client: TestClient, *, tags: list[str] | None = None) -> dict:
    payload: dict = {"name": _unique_name("experiment"), "num_ratings_per_question": 2}
    if tags is not None:
        payload["tags"] = tags
    response = client.post("/api/admin/experiments", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _patch_tags(client: TestClient, experiment_id: int, tags: list[str] | None) -> dict:
    payload: dict = {"assistance_method": "none"}
    if tags is not None:
        payload["tags"] = tags
    response = client.patch(f"/api/admin/experiments/{experiment_id}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _mark_experiment_status(sync_engine, experiment_id: int, status: str) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET status = :status WHERE id = :id"),
            {"status": status, "id": experiment_id},
        )


def test_create_with_tags_returns_them_sorted(client: TestClient):
    created = _create_experiment(client, tags=["zeta", "Alpha"])
    assert created["tags"] == ["Alpha", "zeta"]

    listed = client.get("/api/admin/experiments").json()
    assert listed[0]["tags"] == ["Alpha", "zeta"]


def test_create_without_tags_defaults_to_empty(client: TestClient):
    created = _create_experiment(client)
    assert created["tags"] == []


def test_tag_names_are_normalized_and_deduped(client: TestClient):
    created = _create_experiment(client, tags=["  SWE  Bench ", "swe bench", "vision"])
    assert created["tags"] == ["SWE Bench", "vision"]


def test_tags_are_shared_across_experiments_case_insensitively(client: TestClient):
    _create_experiment(client, tags=["Vision"])
    second = _create_experiment(client, tags=["vision"])

    assert second["tags"] == ["Vision"]
    tags = client.get("/api/admin/tags").json()
    assert tags == [{"name": "Vision", "usage_count": 2}]


def test_tags_endpoint_ranks_by_usage_then_name(client: TestClient):
    _create_experiment(client, tags=["common", "beta"])
    _create_experiment(client, tags=["common", "alpha"])
    _create_experiment(client, tags=["common"])

    tags = client.get("/api/admin/tags").json()
    assert tags == [
        {"name": "common", "usage_count": 3},
        {"name": "alpha", "usage_count": 1},
        {"name": "beta", "usage_count": 1},
    ]


def test_archived_experiments_do_not_count_toward_usage(client: TestClient):
    kept = _create_experiment(client, tags=["needs-review"])
    archived = _create_experiment(client, tags=["needs-review"])
    client.post(f"/api/admin/experiments/{archived['id']}/archive")

    tags = client.get("/api/admin/tags").json()
    assert tags == [{"name": "needs-review", "usage_count": 1}]
    assert kept["tags"] == ["needs-review"]


def test_zero_usage_tags_remain_in_vocabulary(client: TestClient):
    experiment = _create_experiment(client, tags=["ephemeral"])
    _patch_tags(client, experiment["id"], [])

    tags = client.get("/api/admin/tags").json()
    assert tags == [{"name": "ephemeral", "usage_count": 0}]


def test_list_filters_by_tag_case_insensitively(client: TestClient):
    match = _create_experiment(client, tags=["pilot-batch-2"])
    _create_experiment(client, tags=["needs-review"])
    _create_experiment(client)

    hits = client.get("/api/admin/experiments", params={"tag": "Pilot-Batch-2"}).json()
    assert {item["id"] for item in hits} == {match["id"]}


def test_tag_filter_is_exact_not_substring(client: TestClient):
    _create_experiment(client, tags=["vision-v2"])

    hits = client.get("/api/admin/experiments", params={"tag": "vision"}).json()
    assert hits == []


def test_patch_replaces_tag_set_and_omitting_leaves_unchanged(client: TestClient):
    experiment = _create_experiment(client, tags=["old"])

    replaced = _patch_tags(client, experiment["id"], ["new-a", "new-b"])
    assert replaced["tags"] == ["new-a", "new-b"]

    untouched = _patch_tags(client, experiment["id"], None)
    assert untouched["tags"] == ["new-a", "new-b"]


def test_tags_stay_editable_after_config_lock(client: TestClient, sync_engine):
    experiment = _create_experiment(client, tags=["draft-tag"])
    _mark_experiment_status(sync_engine, experiment["id"], "LAUNCH")

    updated = _patch_tags(client, experiment["id"], ["needs-review"])
    assert updated["tags"] == ["needs-review"]


def test_duplicate_copies_tags(client: TestClient):
    source = _create_experiment(client, tags=["needs-review", "pilot-batch-2"])

    duplicate = client.post(f"/api/admin/experiments/{source['id']}/duplicate").json()
    assert duplicate["tags"] == ["needs-review", "pilot-batch-2"]

    tags = {item["name"]: item["usage_count"] for item in client.get("/api/admin/tags").json()}
    assert tags == {"needs-review": 2, "pilot-batch-2": 2}


def test_deleting_experiment_keeps_tag_rows(client: TestClient):
    experiment = _create_experiment(client, tags=["survivor"])
    delete = client.delete(f"/api/admin/experiments/{experiment['id']}")
    assert delete.status_code == 200

    tags = client.get("/api/admin/tags").json()
    assert tags == [{"name": "survivor", "usage_count": 0}]


def test_overlong_tag_name_is_rejected(client: TestClient):
    response = client.post(
        "/api/admin/experiments",
        json={
            "name": _unique_name("experiment"),
            "num_ratings_per_question": 2,
            "tags": ["x" * 65],
        },
    )
    assert response.status_code == 422


def test_too_many_tags_is_rejected(client: TestClient):
    response = client.post(
        "/api/admin/experiments",
        json={
            "name": _unique_name("experiment"),
            "num_ratings_per_question": 2,
            "tags": [f"tag-{i}" for i in range(21)],
        },
    )
    assert response.status_code == 422
