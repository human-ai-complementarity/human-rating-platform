"""End-to-end CRUD for experiment groups and experiment attachment.

Covers wave auto-fill / validation against the dataset set, unique
(dataset × wave) and per-dataset name, lock-after-launch, list filters,
duplicate copying group_id, and RESTRICT deletes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text


def _dataset(client: TestClient, name: str, waves: list[str]) -> dict:
    resp = client.post("/api/admin/datasets", json={"name": name, "waves": waves})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _group(
    client: TestClient,
    name: str,
    dataset_id: int,
    wave: str | None = None,
) -> dict:
    payload: dict = {"name": name, "dataset_id": dataset_id}
    if wave is not None:
        payload["wave"] = wave
    resp = client.post("/api/admin/experiment-groups", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _experiment(client: TestClient, name: str, group_id: int | None = None) -> dict:
    payload: dict = {"name": name}
    if group_id is not None:
        payload["group_id"] = group_id
    resp = client.post("/api/admin/experiments", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _mark_status(sync_engine, experiment_id: int, status: str) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET status = :status WHERE id = :id"),
            {"status": status, "id": experiment_id},
        )


def test_create_autofills_singleton_wave(client: TestClient) -> None:
    dataset = _dataset(client, "medqa", ["fall25"])
    group = _group(client, "MedQA Fall 25", dataset["id"])
    assert group["wave"] == "fall25"
    assert group["dataset_id"] == dataset["id"]
    assert group["dataset_name"] == "medqa"
    assert group["experiment_count"] == 0


def test_create_requires_wave_when_set_is_ambiguous_or_empty(client: TestClient) -> None:
    dual = _dataset(client, "swe-bench", ["fall25", "sp26"])
    missing = client.post(
        "/api/admin/experiment-groups",
        json={"name": "SWE-bench", "dataset_id": dual["id"]},
    )
    assert missing.status_code == 400
    assert "multiple waves" in missing.json()["detail"]

    empty = _dataset(client, "scratch", [])
    no_waves = client.post(
        "/api/admin/experiment-groups",
        json={"name": "Scratch", "dataset_id": empty["id"]},
    )
    assert no_waves.status_code == 400
    assert "no waves" in no_waves.json()["detail"]


def test_wave_must_be_in_dataset_set_and_is_lowercased(client: TestClient) -> None:
    dataset = _dataset(client, "medqa", ["fall25", "sp26"])
    group = _group(client, "MedQA Spring", dataset["id"], wave="SP26")
    assert group["wave"] == "sp26"

    bad = client.post(
        "/api/admin/experiment-groups",
        json={"name": "MedQA Winter", "dataset_id": dataset["id"], "wave": "winter26"},
    )
    assert bad.status_code == 400
    assert "winter26" in bad.json()["detail"]


def test_dataset_wave_pair_is_unique(client: TestClient) -> None:
    dataset = _dataset(client, "medqa", ["fall25", "sp26"])
    _group(client, "MedQA Fall", dataset["id"], "fall25")
    dup = client.post(
        "/api/admin/experiment-groups",
        json={"name": "MedQA Fall again", "dataset_id": dataset["id"], "wave": "fall25"},
    )
    assert dup.status_code == 409

    # Same wave, different dataset is fine.
    other = _dataset(client, "argus", ["fall25"])
    _group(client, "Argus Fall", other["id"], "fall25")


def test_name_unique_per_dataset_case_insensitively(client: TestClient) -> None:
    a = _dataset(client, "medqa", ["fall25", "sp26"])
    b = _dataset(client, "argus", ["fall25"])
    _group(client, "Spring collection", a["id"], "sp26")

    dup = client.post(
        "/api/admin/experiment-groups",
        json={"name": "spring collection", "dataset_id": a["id"], "wave": "fall25"},
    )
    assert dup.status_code == 409

    # Same name on a different dataset is fine.
    other = _group(client, "Spring collection", b["id"], "fall25")
    assert other["name"] == "Spring collection"


def test_list_filters_and_counts(client: TestClient) -> None:
    medqa = _dataset(client, "medqa", ["fall25", "sp26"])
    argus = _dataset(client, "argus", ["fall25"])
    fall = _group(client, "MedQA Fall", medqa["id"], "fall25")
    spring = _group(client, "MedQA Spring", medqa["id"], "sp26")
    _group(client, "Argus Fall", argus["id"], "fall25")
    _experiment(client, "e1", fall["id"])
    _experiment(client, "e2", fall["id"])

    all_rows = client.get("/api/admin/experiment-groups").json()
    assert [r["name"] for r in all_rows] == ["Argus Fall", "MedQA Fall", "MedQA Spring"]
    by_name = {r["name"]: r for r in all_rows}
    assert by_name["MedQA Fall"]["experiment_count"] == 2
    assert by_name["MedQA Spring"]["experiment_count"] == 0

    only_medqa = client.get(
        "/api/admin/experiment-groups", params={"dataset_id": medqa["id"]}
    ).json()
    assert [r["name"] for r in only_medqa] == ["MedQA Fall", "MedQA Spring"]

    only_fall = client.get("/api/admin/experiment-groups", params={"wave": "Fall25"}).json()
    assert {r["name"] for r in only_fall} == {"Argus Fall", "MedQA Fall"}

    fetched = client.get(f"/api/admin/experiment-groups/{spring['id']}").json()
    assert fetched["id"] == spring["id"]
    assert client.get("/api/admin/experiment-groups/9999").status_code == 404


def test_patch_name_and_wave_on_unlocked_group(client: TestClient) -> None:
    dataset = _dataset(client, "medqa", ["fall25", "sp26"])
    group = _group(client, "MedQA Fall", dataset["id"], "fall25")

    renamed = client.patch(
        f"/api/admin/experiment-groups/{group['id']}", json={"name": "MedQA Fall 25"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "MedQA Fall 25"
    assert renamed.json()["wave"] == "fall25"

    moved = client.patch(f"/api/admin/experiment-groups/{group['id']}", json={"wave": "sp26"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["wave"] == "sp26"
    assert moved.json()["name"] == "MedQA Fall 25"


def test_unlocked_dataset_move_preserves_shared_wave(client: TestClient) -> None:
    source = _dataset(client, "medqa", ["fall25", "sp26"])
    dest = _dataset(client, "argus", ["fall25", "winter26"])
    group = _group(client, "MedQA Fall", source["id"], "fall25")

    moved = client.patch(
        f"/api/admin/experiment-groups/{group['id']}", json={"dataset_id": dest["id"]}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["dataset_id"] == dest["id"]
    assert moved.json()["dataset_name"] == "argus"
    assert moved.json()["wave"] == "fall25"
    assert moved.json()["name"] == "MedQA Fall"


def test_unlocked_dataset_move_without_shared_wave_requires_pick(client: TestClient) -> None:
    source = _dataset(client, "medqa", ["fall25", "sp26"])
    dest = _dataset(client, "argus", ["sp26", "winter26"])
    group = _group(client, "MedQA Fall", source["id"], "fall25")

    moved = client.patch(
        f"/api/admin/experiment-groups/{group['id']}", json={"dataset_id": dest["id"]}
    )
    assert moved.status_code == 400
    assert "multiple waves" in moved.json()["detail"]


def test_lock_blocks_dataset_and_wave_but_not_name(client: TestClient, sync_engine) -> None:
    dataset = _dataset(client, "medqa", ["fall25", "sp26"])
    other = _dataset(client, "argus", ["fall25"])
    group = _group(client, "MedQA Fall", dataset["id"], "fall25")
    exp = _experiment(client, "main", group["id"])
    _mark_status(sync_engine, exp["id"], "LAUNCH")

    wave = client.patch(f"/api/admin/experiment-groups/{group['id']}", json={"wave": "sp26"})
    assert wave.status_code == 400
    assert "locked" in wave.json()["detail"]

    dataset_move = client.patch(
        f"/api/admin/experiment-groups/{group['id']}", json={"dataset_id": other["id"]}
    )
    assert dataset_move.status_code == 400

    # Same-value no-ops still succeed (frontend re-sends unchanged fields).
    noop = client.patch(f"/api/admin/experiment-groups/{group['id']}", json={"wave": "fall25"})
    assert noop.status_code == 200, noop.text

    renamed = client.patch(
        f"/api/admin/experiment-groups/{group['id']}", json={"name": "MedQA Fall (locked)"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "MedQA Fall (locked)"
    assert renamed.json()["wave"] == "fall25"


def test_attach_experiment_inherits_group_and_filters(client: TestClient) -> None:
    dataset = _dataset(client, "medqa", ["fall25", "sp26"])
    group = _group(client, "MedQA Fall", dataset["id"], "fall25")
    other = _group(client, "MedQA Spring", dataset["id"], "sp26")
    attached = _experiment(client, "with-group", group["id"])
    _experiment(client, "ungrouped")

    assert attached["group_id"] == group["id"]
    assert attached["group_name"] == "MedQA Fall"
    assert attached["group_dataset_id"] == dataset["id"]
    assert attached["group_dataset_name"] == "medqa"
    assert attached["wave"] == "fall25"

    listed = client.get("/api/admin/experiments", params={"group_id": group["id"]}).json()
    assert [e["name"] for e in listed] == ["with-group"]

    by_wave = client.get("/api/admin/experiments", params={"wave": "fall25"}).json()
    assert [e["name"] for e in by_wave] == ["with-group"]

    by_dataset = client.get("/api/admin/experiments", params={"dataset_id": dataset["id"]}).json()
    assert {e["name"] for e in by_dataset} == {"with-group"}

    # Reassign while DRAFT, then ungroup with an explicit null.
    moved = client.patch(
        f"/api/admin/experiments/{attached['id']}",
        json={"assistance_method": "none", "group_id": other["id"]},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["wave"] == "sp26"
    assert moved.json()["group_name"] == "MedQA Spring"

    cleared = client.patch(
        f"/api/admin/experiments/{attached['id']}",
        json={"assistance_method": "none", "group_id": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["group_id"] is None
    assert cleared.json()["wave"] is None


def test_group_id_locked_after_launch(client: TestClient, sync_engine) -> None:
    dataset = _dataset(client, "medqa", ["fall25", "sp26"])
    group = _group(client, "MedQA Fall", dataset["id"], "fall25")
    other = _group(client, "MedQA Spring", dataset["id"], "sp26")
    exp = _experiment(client, "main", group["id"])
    _mark_status(sync_engine, exp["id"], "LAUNCH")

    moved = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={"assistance_method": "none", "group_id": other["id"]},
    )
    assert moved.status_code == 400
    assert "group_id" in moved.json()["detail"]

    # Re-sending the current group_id is a no-op and must not trip the lock.
    noop = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={"assistance_method": "none", "group_id": group["id"]},
    )
    assert noop.status_code == 200, noop.text


def test_duplicate_copies_group(client: TestClient) -> None:
    dataset = _dataset(client, "medqa", ["fall25"])
    group = _group(client, "MedQA Fall", dataset["id"])
    source = _experiment(client, "main", group["id"])

    dup = client.post(f"/api/admin/experiments/{source['id']}/duplicate")
    assert dup.status_code == 200, dup.text
    assert dup.json()["group_id"] == group["id"]
    assert dup.json()["wave"] == "fall25"
    assert dup.json()["status"] == "DRAFT"


def test_unknown_group_on_create_is_404(client: TestClient) -> None:
    resp = client.post("/api/admin/experiments", json={"name": "x", "group_id": 9999})
    assert resp.status_code == 404


def test_delete_group_blocked_while_experiments_exist(client: TestClient) -> None:
    dataset = _dataset(client, "medqa", ["fall25"])
    group = _group(client, "MedQA Fall", dataset["id"])
    exp = _experiment(client, "main", group["id"])

    blocked = client.delete(f"/api/admin/experiment-groups/{group['id']}")
    assert blocked.status_code == 409

    client.delete(f"/api/admin/experiments/{exp['id']}")
    assert client.delete(f"/api/admin/experiment-groups/{group['id']}").status_code == 200
    assert client.get("/api/admin/experiment-groups").json() == []


def test_delete_dataset_blocked_while_groups_exist(client: TestClient) -> None:
    dataset = _dataset(client, "medqa", ["fall25"])
    group = _group(client, "MedQA Fall", dataset["id"])

    blocked = client.delete(f"/api/admin/datasets/{dataset['id']}")
    assert blocked.status_code == 409

    client.delete(f"/api/admin/experiment-groups/{group['id']}")
    assert client.delete(f"/api/admin/datasets/{dataset['id']}").status_code == 200


def test_cannot_remove_wave_still_used_by_a_group(client: TestClient) -> None:
    dataset = _dataset(client, "medqa", ["fall25", "sp26"])
    _group(client, "MedQA Fall", dataset["id"], "fall25")

    shrink = client.patch(f"/api/admin/datasets/{dataset['id']}", json={"waves": ["sp26"]})
    assert shrink.status_code == 400
    assert "fall25" in shrink.json()["detail"]

    # Adding a wave (or keeping the in-use one) is fine.
    widen = client.patch(
        f"/api/admin/datasets/{dataset['id']}", json={"waves": ["fall25", "sp26", "fa26"]}
    )
    assert widen.status_code == 200, widen.text
    assert widen.json()["waves"] == ["fall25", "sp26", "fa26"]
