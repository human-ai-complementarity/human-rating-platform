"""Catalog seed + experiment-group backfill."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from services.admin.catalog import PIPELINE_DATASETS


def _create_experiment(client: TestClient, name: str) -> dict:
    response = client.post("/api/admin/experiments", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


def _upload(client: TestClient, experiment_id: int, filename: str) -> None:
    csv_data = (
        "question_id,question_text,gt_answer,options,question_type\n"
        "q1,Is this useful?,Yes,Yes|No,MC\n"
    )
    response = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": (filename, csv_data, "text/csv")},
    )
    assert response.status_code == 200, response.text


def _sync(client: TestClient) -> dict:
    response = client.post("/api/admin/catalog/sync")
    assert response.status_code == 200, response.text
    return response.json()


def test_get_catalog_returns_scheduled_cards(client: TestClient):
    rows = client.get("/api/admin/catalog").json()
    assert [(row["name"], tuple(row["waves"])) for row in rows] == list(PIPELINE_DATASETS)


def test_sync_seeds_datasets_and_is_idempotent(client: TestClient):
    first = _sync(client)
    assert sorted(first["datasets_created"]) == sorted(name for name, _ in PIPELINE_DATASETS)
    assert first["datasets_updated"] == []
    assert first["experiments_assigned"] == []

    datasets = client.get("/api/admin/datasets").json()
    by_name = {row["name"]: row["waves"] for row in datasets}
    assert by_name["QuALITY_dev"] == ["fall25"]
    assert by_name["bbeh_mini"] == ["fall25", "sp26"]
    assert by_name["culturalbench_hard"] == ["sp26"]

    second = _sync(client)
    assert second["datasets_created"] == []
    assert second["datasets_updated"] == []
    assert len(client.get("/api/admin/datasets").json()) == len(PIPELINE_DATASETS)


def test_sync_unions_catalog_waves_onto_existing_dataset(client: TestClient):
    created = client.post(
        "/api/admin/datasets", json={"name": "bbeh_mini", "waves": ["fall25"]}
    ).json()
    assert created["waves"] == ["fall25"]

    result = _sync(client)
    assert "bbeh_mini" in result["datasets_updated"]
    fetched = client.get(f"/api/admin/datasets/{created['id']}").json()
    assert fetched["waves"] == ["fall25", "sp26"]
    assert fetched["name"] == "bbeh_mini"


def test_sync_assigns_singleton_wave_from_filename(client: TestClient):
    experiment = _create_experiment(client, "CulturalBench run")
    _upload(client, experiment["id"], "culturalbench_hard_n300.csv")

    result = _sync(client)
    assigned = result["experiments_assigned"]
    assert len(assigned) == 1
    assert assigned[0]["experiment_id"] == experiment["id"]
    assert assigned[0]["dataset_name"] == "culturalbench_hard"
    assert assigned[0]["wave"] == "sp26"
    assert any(name.startswith("culturalbench_hard sp26") for name in result["groups_created"])

    listed = client.get("/api/admin/experiments").json()
    row = next(item for item in listed if item["id"] == experiment["id"])
    assert row["dataset_name"] == "culturalbench_hard"
    assert row["wave"] == "sp26"
    assert row["group_name"] == "culturalbench_hard sp26"


def test_sync_assigns_dual_wave_when_name_has_token(client: TestClient):
    experiment = _create_experiment(client, "bbeh mini sp26 rerun")
    _upload(client, experiment["id"], "bbeh_mini_n50.csv")

    result = _sync(client)
    assigned = result["experiments_assigned"]
    assert assigned[0]["dataset_name"] == "bbeh_mini"
    assert assigned[0]["wave"] == "sp26"


def test_sync_skips_dual_wave_without_signal(client: TestClient):
    experiment = _create_experiment(client, "bbeh mini mystery")
    _upload(client, experiment["id"], "bbeh_mini_n50.csv")

    result = _sync(client)
    assert result["experiments_assigned"] == []
    skipped = {item["experiment_id"]: item["reason"] for item in result["experiments_skipped"]}
    assert skipped[experiment["id"]] == "ambiguous_wave"

    listed = client.get("/api/admin/experiments").json()
    row = next(item for item in listed if item["id"] == experiment["id"])
    assert row["group_id"] is None


def test_sync_skips_unrelated_upload(client: TestClient):
    experiment = _create_experiment(client, "scratch draft")
    _upload(client, experiment["id"], "questions.csv")

    result = _sync(client)
    skipped = {item["experiment_id"]: item["reason"] for item in result["experiments_skipped"]}
    assert skipped[experiment["id"]] == "no_upload_match"


def test_sync_prefers_longest_card_name(client: TestClient):
    experiment = _create_experiment(client, "abstracted run")
    _upload(client, experiment["id"], "safeagentbench_abstracted_n10.csv")

    result = _sync(client)
    assert result["experiments_assigned"][0]["dataset_name"] == "safeagentbench_abstracted"


def test_sync_assigns_launched_experiments(client: TestClient, sync_engine):
    experiment = _create_experiment(client, "Launched CulturalBench")
    _upload(client, experiment["id"], "culturalbench_hard_n300.csv")
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET status = 'LAUNCH' WHERE id = :id"),
            {"id": experiment["id"]},
        )

    result = _sync(client)
    assert result["experiments_assigned"][0]["experiment_id"] == experiment["id"]

    listed = client.get("/api/admin/experiments").json()
    row = next(item for item in listed if item["id"] == experiment["id"])
    assert row["status"] == "LAUNCH"
    assert row["group_name"] == "culturalbench_hard sp26"


def test_sync_leaves_already_grouped_experiments_alone(client: TestClient):
    seed = _sync(client)
    dataset_id = next(
        row["id"]
        for row in client.get("/api/admin/datasets").json()
        if row["name"] == "gpqa_diamond"
    )
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": "GPQA Fall", "dataset_id": dataset_id, "wave": "fall25"},
    ).json()
    experiment = client.post(
        "/api/admin/experiments",
        json={"name": "already grouped", "group_id": group["id"]},
    ).json()
    _upload(client, experiment["id"], "gpqa_diamond_n20.csv")

    result = _sync(client)
    assert seed["datasets_created"]
    assert all(item["experiment_id"] != experiment["id"] for item in result["experiments_assigned"])

    listed = client.get("/api/admin/experiments").json()
    row = next(item for item in listed if item["id"] == experiment["id"])
    assert row["group_id"] == group["id"]
    assert row["group_name"] == "GPQA Fall"
