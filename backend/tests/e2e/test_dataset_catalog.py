"""Dataset catalog: one-time backfill of past collections into datasets and groups."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings
from models import Dataset, Experiment, ExperimentGroup
from services.admin.dataset_catalog import (
    COLLECTIONS,
    PIPELINE_DATASETS,
    Collection,
    _insert_or_get_dataset,
    _insert_or_get_group,
    sync_dataset_catalog,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _create_experiment(client: TestClient, name: str, **fields) -> dict:
    response = client.post("/api/admin/experiments", json={"name": name, **fields})
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


def _async_session_maker():
    engine = create_async_engine(get_settings().async_database_url)
    return engine, async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def _sync(*, apply: bool = True, collections: Mapping[int, Collection] | None = None) -> dict:
    """Run the sync in its own session, as the script does, and return the report."""

    async def run():
        engine, session_maker = _async_session_maker()
        try:
            async with session_maker() as db:
                if collections is None:
                    return await sync_dataset_catalog(db, apply=apply)
                return await sync_dataset_catalog(db, apply=apply, collections=collections)
        finally:
            await engine.dispose()

    return asdict(asyncio.run(run()))


def _experiment_row(client: TestClient, experiment_id: int) -> dict:
    return next(
        item
        for item in client.get("/api/admin/experiments?include_archived=true").json()
        if item["id"] == experiment_id
    )


def _skips(report: dict) -> dict[int, str]:
    return {item["experiment_id"]: item["reason"] for item in report["experiments_skipped"]}


def _datasets(client: TestClient) -> dict[str, list[str]]:
    return {row["name"]: row["waves"] for row in client.get("/api/admin/datasets").json()}


def test_sync_seeds_the_roster_and_is_idempotent(client: TestClient):
    first = _sync()
    assert sorted(first["datasets_created"]) == sorted(name for name, _ in PIPELINE_DATASETS)
    assert first["datasets_updated"] == []
    assert first["experiments_assigned"] == []
    # None of the recorded collections exist in this database.
    assert first["manifest_missing"] == sorted(COLLECTIONS)

    by_name = _datasets(client)
    assert by_name["QuALITY_dev"] == ["fall25"]
    assert by_name["shade_arena"] == ["fall25", "sp26"]
    assert by_name["longbenchv2"] == ["sum26"]
    # A collected-only card gets a row only when one of its collections is here.
    assert "safeagentbench_abstracted" not in by_name

    second = _sync()
    assert second["datasets_created"] == []
    assert second["datasets_updated"] == []
    assert len(_datasets(client)) == len(PIPELINE_DATASETS)


def test_sync_unions_catalog_waves_onto_existing_dataset(client: TestClient):
    created = client.post(
        "/api/admin/datasets", json={"name": "shade_arena", "waves": ["fall25"]}
    ).json()
    assert created["waves"] == ["fall25"]

    result = _sync()
    assert "shade_arena" in result["datasets_updated"]
    fetched = client.get(f"/api/admin/datasets/{created['id']}").json()
    assert fetched["waves"] == ["fall25", "sp26"]
    assert fetched["name"] == "shade_arena"


def test_a_listed_collection_is_assigned_to_its_recorded_wave(client: TestClient):
    experiment = _create_experiment(client, "CulturalBench run")
    _upload(client, experiment["id"], "culturalbench_hard_n300.csv")

    result = _sync(collections={experiment["id"]: Collection("culturalbench_hard", "none", "sp26")})
    assigned = result["experiments_assigned"]
    assert [(a["experiment_id"], a["dataset_name"], a["arm"], a["wave"]) for a in assigned] == [
        (experiment["id"], "culturalbench_hard", "none", "sp26")
    ]
    assert result["groups_created"] == ["culturalbench_hard sp26"]

    row = _experiment_row(client, experiment["id"])
    assert row["group_dataset_name"] == "culturalbench_hard"
    assert row["wave"] == "sp26"
    assert row["group_name"] == "culturalbench_hard sp26"


def test_the_recorded_wave_wins_over_the_cards_current_schedule(client: TestClient):
    """longbenchv2 was collected in sp26 and moved to sum26 afterwards.

    The group records the collection's wave, and the dataset's wave set gains
    it — without that, resolve_attribution_wave would refuse sp26 and abort the
    whole pass.
    """
    experiment = _create_experiment(client, "SPAR - Long Bench V2 - Baseline")
    _upload(client, experiment["id"], "longbenchv2_n392.csv")

    result = _sync(collections={experiment["id"]: Collection("longbenchv2", "none", "sp26")})
    assert [a["wave"] for a in result["experiments_assigned"]] == ["sp26"]
    assert result["collected_outside_schedule"] == ["longbenchv2 sp26"]
    assert sorted(_datasets(client)["longbenchv2"]) == ["sp26", "sum26"]
    assert _experiment_row(client, experiment["id"])["group_name"] == "longbenchv2 sp26"


def test_a_collected_card_outside_the_roster_gets_its_own_dataset(client: TestClient):
    """safeagentbench_abstracted: collected in sp26, descheduled, still distinct.

    Its exports must land on its own dataset, not on safeagentbench — under a
    `{card}_*` prefix rule they attached to the shorter card, and assignment
    bypasses the post-launch lock, so that would be unrecoverable.
    """
    abstracted = _create_experiment(client, "abstracted baseline")
    _upload(client, abstracted["id"], "safeagentbench_abstracted_n100.csv")
    plain = _create_experiment(client, "plain baseline")
    _upload(client, plain["id"], "safeagentbench_n640.csv")

    result = _sync(
        collections={
            abstracted["id"]: Collection("safeagentbench_abstracted", "none", "sp26"),
            plain["id"]: Collection("safeagentbench", "none", "sp26"),
        }
    )
    assigned = {a["experiment_id"]: a["dataset_name"] for a in result["experiments_assigned"]}
    assert assigned == {
        abstracted["id"]: "safeagentbench_abstracted",
        plain["id"]: "safeagentbench",
    }
    assert "safeagentbench_abstracted" in result["datasets_created"]
    assert _datasets(client)["safeagentbench_abstracted"] == ["sp26"]


def test_unlisted_experiments_are_reported_and_left_alone(client: TestClient):
    experiment = _create_experiment(client, "a run nobody recorded")
    _upload(client, experiment["id"], "culturalbench_hard_n300.csv")

    result = _sync(collections={})
    assert result["experiments_assigned"] == []
    assert _skips(result) == {experiment["id"]: "not_in_manifest"}
    assert _experiment_row(client, experiment["id"])["group_id"] is None


def test_each_cross_check_refuses_a_disagreeing_experiment(client: TestClient, sync_engine):
    listed = Collection("culturalbench_hard", "none", "sp26")
    wrong_card = _create_experiment(client, "wrong card")
    _upload(client, wrong_card["id"], "shade_arena_n106.csv")
    unknown_file = _create_experiment(client, "primevul variant")
    _upload(client, unknown_file["id"], "primevul_cwe_n300.csv")
    wrong_arm = _create_experiment(client, "wrong arm", assistance_method="top_n")
    _upload(client, wrong_arm["id"], "culturalbench_hard_n300.csv")
    wrong_wave = _create_experiment(client, "CulturalBench fall25 rerun")
    _upload(client, wrong_wave["id"], "culturalbench_hard_n300.csv")
    archived = _create_experiment(client, "archived run")
    _upload(client, archived["id"], "culturalbench_hard_n300.csv")
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET archived_at = now() WHERE id = :id"),
            {"id": archived["id"]},
        )

    result = _sync(
        collections={
            wrong_card["id"]: listed,
            unknown_file["id"]: Collection("primevul", "none", "sum26"),
            wrong_arm["id"]: listed,
            wrong_wave["id"]: listed,
            archived["id"]: listed,
        }
    )
    assert result["experiments_assigned"] == []
    assert _skips(result) == {
        wrong_card["id"]: "card_mismatch",
        unknown_file["id"]: "card_mismatch",
        wrong_arm["id"]: "arm_mismatch",
        wrong_wave["id"]: "wave_conflict",
        archived["id"]: "archived",
    }
    assert result["groups_created"] == []
    # Nothing was attributed, so no collected wave was claimed for a dataset.
    assert result["datasets_updated"] == []
    assert result["collected_outside_schedule"] == []


def test_assigned_collections_are_left_alone_on_a_rerun(client: TestClient):
    experiment = _create_experiment(client, "CulturalBench run")
    _upload(client, experiment["id"], "culturalbench_hard_n300.csv")
    collections = {experiment["id"]: Collection("culturalbench_hard", "none", "sp26")}

    first = _sync(collections=collections)
    group_id = first["experiments_assigned"][0]["group_id"]

    second = _sync(collections=collections)
    assert second["experiments_assigned"] == []
    assert _skips(second) == {experiment["id"]: "already_grouped"}
    assert second["groups_created"] == []
    assert _experiment_row(client, experiment["id"])["group_id"] == group_id


def test_a_listed_experiment_grouped_by_hand_is_not_moved(client: TestClient):
    _sync()
    dataset_id = next(
        row["id"]
        for row in client.get("/api/admin/datasets").json()
        if row["name"] == "culturalbench_hard"
    )
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": "Hand-made", "dataset_id": dataset_id, "wave": "sp26"},
    ).json()
    experiment = _create_experiment(client, "grouped by hand", group_id=group["id"])
    _upload(client, experiment["id"], "culturalbench_hard_n300.csv")

    result = _sync(collections={experiment["id"]: Collection("culturalbench_hard", "none", "sp26")})
    assert _skips(result) == {experiment["id"]: "already_grouped"}
    assert _experiment_row(client, experiment["id"])["group_name"] == "Hand-made"


def test_a_launched_collection_is_attached(client: TestClient, sync_engine):
    experiment = _create_experiment(client, "Launched CulturalBench")
    _upload(client, experiment["id"], "culturalbench_hard_n300.csv")
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE experiments SET status = 'LAUNCH' WHERE id = :id"),
            {"id": experiment["id"]},
        )

    result = _sync(collections={experiment["id"]: Collection("culturalbench_hard", "none", "sp26")})
    assert result["experiments_assigned"][0]["experiment_id"] == experiment["id"]

    row = _experiment_row(client, experiment["id"])
    assert row["status"] == "LAUNCH"
    assert row["group_name"] == "culturalbench_hard sp26"


def test_a_later_wave_on_the_same_dataset_is_a_new_group(client: TestClient):
    """Re-collecting a dataset in a later wave is a second group, not a regroup."""
    sp26 = _create_experiment(client, "shade arena, spring")
    _upload(client, sp26["id"], "shade_arena_n106.csv")
    fall25 = _create_experiment(client, "shade arena, earlier")
    _upload(client, fall25["id"], "shade_arena_n50.csv")

    result = _sync(
        collections={
            sp26["id"]: Collection("shade_arena", "none", "sp26"),
            fall25["id"]: Collection("shade_arena", "none", "fall25"),
        }
    )
    assert sorted(result["groups_created"]) == ["shade_arena fall25", "shade_arena sp26"]
    assert _experiment_row(client, sp26["id"])["wave"] == "sp26"
    assert _experiment_row(client, fall25["id"])["wave"] == "fall25"


def test_catalog_dataset_unique_race_reuses_row_and_keeps_sibling_insert():
    """A concurrent insert of the same dataset must not 500 or abort the caller."""

    async def _run() -> None:
        engine, Session = _async_session_maker()
        try:
            async with Session() as setup:
                setup.add(Dataset(name="gpqa_diamond", waves=json.dumps(["fall25"])))
                await setup.commit()

            async with Session() as db:
                probe = Dataset(name="probe_dataset", waves=json.dumps(["sp26"]))
                db.add(probe)
                await db.flush()
                dataset, created = await _insert_or_get_dataset("gpqa_diamond", ["fall25"], db)
                await db.commit()
                assert created is False
                assert dataset.name == "gpqa_diamond"
                probe_id = probe.id

            async with Session() as verify:
                assert (await verify.get(Dataset, probe_id)) is not None
                names = [
                    row.name
                    for row in (await verify.execute(select(Dataset))).scalars().all()
                    if row.name.lower() == "gpqa_diamond"
                ]
                assert names == ["gpqa_diamond"]
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_catalog_group_unique_race_reuses_row_and_keeps_the_experiment():
    """A concurrent insert of the same dataset×wave must not 500 or abort the caller."""

    async def _run() -> None:
        engine, Session = _async_session_maker()
        try:
            async with Session() as setup:
                dataset = Dataset(name="gpqa_diamond", waves=json.dumps(["fall25"]))
                setup.add(dataset)
                await setup.flush()
                setup.add(
                    ExperimentGroup(
                        name="gpqa_diamond fall25",
                        dataset_id=dataset.id,
                        wave="fall25",
                    )
                )
                await setup.commit()
                dataset_id = dataset.id

            async with Session() as db:
                experiment = Experiment(name="probe-exp", num_ratings_per_question=1)
                db.add(experiment)
                await db.flush()
                group, created = await _insert_or_get_group(
                    db,
                    ExperimentGroup(
                        name="gpqa_diamond fall25",
                        dataset_id=dataset_id,
                        wave="fall25",
                    ),
                )
                await db.commit()
                assert created is False
                assert group.wave == "fall25"
                experiment_id = experiment.id

            async with Session() as verify:
                assert (await verify.get(Experiment, experiment_id)) is not None
                rows = (
                    (
                        await verify.execute(
                            select(ExperimentGroup).where(
                                ExperimentGroup.dataset_id == dataset_id,
                                ExperimentGroup.wave == "fall25",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(rows) == 1
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_sync_survives_a_group_name_collision_on_both_candidates(client: TestClient):
    """Both generated names taken by human-renamed groups must not crash the sync.

    `_get_or_create_group` falls back from "{dataset} {wave}" to
    "{dataset} ({wave})" without checking the second name is free, and the
    IntegrityError recovery only re-queries by (dataset_id, wave) — so a
    collision on the name index used to be mis-diagnosed and re-raised.
    """
    _sync()
    dataset_id = next(
        row["id"] for row in client.get("/api/admin/datasets").json() if row["name"] == "bbeh_mini"
    )
    # Admin widens the wave set, then renames two groups onto the names the
    # backfill would generate for sp26.
    client.patch(
        f"/api/admin/datasets/{dataset_id}",
        json={"waves": ["fall25", "sp26", "sum26"]},
    )
    for wave, name in (("fall25", "bbeh_mini sp26"), ("sum26", "bbeh_mini (sp26)")):
        created = client.post(
            "/api/admin/experiment-groups",
            json={"name": f"tmp {wave}", "dataset_id": dataset_id, "wave": wave},
        )
        assert created.status_code == 200, created.text
        renamed = client.patch(
            f"/api/admin/experiment-groups/{created.json()['id']}", json={"name": name}
        )
        assert renamed.status_code == 200, renamed.text

    experiment = _create_experiment(client, "bbeh run")
    _upload(client, experiment["id"], "bbeh_mini_n40.csv")
    collections = {experiment["id"]: Collection("bbeh_mini", "none", "sp26")}

    body = _sync(collections=collections)

    # Reported as a skip, and the experiment is left ungrouped for a human.
    assert _skips(body) == {experiment["id"]: "group_name_conflict"}
    assert body["experiments_assigned"] == []
    assert _experiment_row(client, experiment["id"])["group_id"] is None

    # Still idempotent.
    again = _sync(collections=collections)
    assert _skips(again) == {experiment["id"]: "group_name_conflict"}

    # Freeing one of the two names lets a later sync place it.
    groups = client.get("/api/admin/experiment-groups").json()
    clash = next(group for group in groups if group["name"] == "bbeh_mini (sp26)")
    client.patch(f"/api/admin/experiment-groups/{clash['id']}", json={"name": "bbeh_mini summer"})

    final = _sync(collections=collections)
    assert [a["experiment_id"] for a in final["experiments_assigned"]] == [experiment["id"]]


def test_dry_run_writes_nothing_and_reports_exactly_what_apply_writes(client: TestClient):
    experiment = _create_experiment(client, "Long Bench run")
    _upload(client, experiment["id"], "longbenchv2_n392.csv")
    collections = {experiment["id"]: Collection("longbenchv2", "none", "sp26")}
    datasets_before = client.get("/api/admin/datasets").json()
    groups_before = client.get("/api/admin/experiment-groups").json()

    dry = _sync(apply=False, collections=collections)
    assert dry["applied"] is False
    assert [a["experiment_id"] for a in dry["experiments_assigned"]] == [experiment["id"]]
    assert client.get("/api/admin/datasets").json() == datasets_before
    assert client.get("/api/admin/experiment-groups").json() == groups_before
    assert _experiment_row(client, experiment["id"])["group_id"] is None

    applied = _sync(collections=collections)
    assert applied["applied"] is True

    def comparable(report: dict) -> tuple:
        # Ids of groups created in a dry run belong to rolled-back rows, so
        # compare by name.
        return (
            sorted(report["datasets_created"]),
            sorted(report["datasets_updated"]),
            sorted(report["groups_created"]),
            sorted(
                (item["experiment_id"], item["dataset_name"], item["wave"], item["group_name"])
                for item in report["experiments_assigned"]
            ),
            sorted(
                (item["experiment_id"], item["reason"]) for item in report["experiments_skipped"]
            ),
        )

    assert comparable(dry) == comparable(applied)
    assert _experiment_row(client, experiment["id"])["group_name"] == "longbenchv2 sp26"


def _insert_experiment(conn, experiment_id: int, name: str, arm: str, filename: str) -> None:
    """An experiment at a fixed id, as the recorded collection names it."""
    conn.execute(
        text(
            "INSERT INTO experiments (id, name, internal_name, num_ratings_per_question, "
            "assistance_method) VALUES (:id, :name, :name, 3, :arm)"
        ),
        {"id": experiment_id, "name": name, "arm": arm},
    )
    conn.execute(
        text(
            "INSERT INTO uploads (experiment_id, filename, question_count) "
            "VALUES (:id, :filename, 1)"
        ),
        {"id": experiment_id, "filename": filename},
    )


def test_script_is_a_dry_run_by_default_and_writes_only_with_apply(client: TestClient, sync_engine):
    # The script always runs the real record, so use one of its ids.
    with sync_engine.begin() as conn:
        _insert_experiment(
            conn, 133, "SPAR - Long Bench V2 - Baseline", "none", "longbenchv2_n392.csv"
        )
    datasets_before = client.get("/api/admin/datasets").json()

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/sync_dataset_catalog.py", *args],
            cwd=BACKEND_DIR,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )

    expected_line = '#133 "SPAR - Long Bench V2 - Baseline" -> longbenchv2 sp26, arm none'

    dry = run()
    assert dry.returncode == 0, dry.stderr
    assert "DRY RUN" in dry.stdout
    assert expected_line in dry.stdout
    assert client.get("/api/admin/datasets").json() == datasets_before
    assert _experiment_row(client, 133)["group_id"] is None

    applied = run("--apply")
    assert applied.returncode == 0, applied.stderr
    assert "APPLIED" in applied.stdout
    assert expected_line in applied.stdout
    assert _experiment_row(client, 133)["group_name"] == "longbenchv2 sp26"


# Production's 39 experiments as of 2026-09-28: id, internal name, upload,
# assistance method, archived. Read-only snapshot; used to check the recorded
# collections against what production actually holds.
PRODUCTION = (
    (
        20,
        "Difference Awareness Baseline",
        "multidimensional_difference_awareness_n64.parquet",
        "none",
        True,
    ),
    (22, "Liars Bench Baseline", "liars_bench_n464.parquet", "none", True),
    (31, "SPAR - BBEH Mini - Baseline", "bbeh_mini_n376.csv", "none", True),
    (65, "SPAR -  FACTS Search -  Baseline", "facts_search_public_n300.csv", "none", False),
    (67, "SPAR - safeagentbench -  baseline", "safeagentbench_n640.csv", "none", False),
    (
        68,
        "SPAR - safeagentbench -abstracted - baseline",
        "safeagentbench_abstracted_n100.csv",
        "none",
        False,
    ),
    (71, "SPAR - Shade Arena - Baseline", "shade_arena_n106.csv", "none", False),
    (
        72,
        "SPAR - multidimensional_difference_awareness - baseline",
        "multidimensional_difference_awareness_n120.csv",
        "none",
        False,
    ),
    (75, "SPAR - liars_bench - baseline", "liars_bench_n464_fixed.csv", "none", False),
    (
        76,
        "SUM26 FindTheFlaws CELS Lojban (match) — baseline — dipo101",
        "find_the_flaws_cels_lojban_match_n88.parquet",
        "none",
        False,
    ),
    (
        78,
        "SUM26 - MARS - find_the_flaws_modified_gpqa_flaw  - baseline - dipo101",
        "find_the_flaws_modified_gpqa_flaw_n122.parquet",
        "none",
        False,
    ),
    (
        80,
        "SPAR - safeagentbench -abstracted - ISD",
        "safeagentbench_abstracted_n100.csv",
        "human_as_a_tool",
        False,
    ),
    (
        81,
        "SPAR - safeagentbench -abstracted - Top-3",
        "safeagentbench_abstracted_n100.csv",
        "top_n",
        True,
    ),
    (82, "SPAR -  CulturalBench Hard -  Baseline", "culturalbench_hard_n1227.csv", "none", False),
    (
        83,
        "SPAR -  AttuneBench Pairwise -  Baseline",
        "attunebench_pairwise_n800.csv",
        "none",
        False,
    ),
    (84, "SPAR -  FACTS Search - ISD", "facts_search_public_n300.csv", "human_as_a_tool", False),
    (85, "SPAR - Shade Arena -  ISD", "shade_arena_n106.csv", "human_as_a_tool", False),
    (117, "SPAR -  SafeAgentBench -  ISD", "safeagentbench_n640.csv", "human_as_a_tool", False),
    (119, "SPAR -  BBEH Mini -  ISD", "bbeh_mini_n376.csv", "human_as_a_tool", True),
    (
        120,
        "SPAR -  Multidimensional Difference Awareness -  ISD",
        "multidimensional_difference_awareness_n120.csv",
        "human_as_a_tool",
        False,
    ),
    (
        121,
        "SPAR - AttuneBench Pairwise -  ISD",
        "attunebench_pairwise_n800.csv",
        "human_as_a_tool",
        False,
    ),
    (122, "SPAR -  Liars Bench -  ISD", "liars_bench_n464.csv", "human_as_a_tool", False),
    (
        123,
        "SPAR - CulturalBench Hard -  ISD",
        "culturalbench_hard_n1227.csv",
        "human_as_a_tool",
        False,
    ),
    (124, "SPAR -  FACTS Search -  Top-3", "facts_search_public_n300.csv", "top_n", False),
    (125, "SPAR - Liars Bench - Top-3", "liars_bench_n464_fixed.csv", "top_n", False),
    (126, "SPAR -  Shade Arena -  Top-3", "shade_arena_n106.csv", "top_n", False),
    (127, "SPAR -  SafeAgent Bench -  Top-3", "safeagentbench_n640.csv", "top_n", False),
    (
        128,
        "SPAR -  SafeAgent Bench abstracted -  Top-3",
        "safeagentbench_abstracted_n100.csv",
        "top_n",
        False,
    ),
    (129, "SPAR - CulturalBench Hard -  Top-3", "culturalbench_hard_n1227.csv", "top_n", False),
    (130, "SPAR - AttuneBench Pairwise -  Top-3", "attunebench_pairwise_n800.csv", "top_n", False),
    (
        131,
        "SPAR - Multidimensional Difference Awareness -  Top-3",
        "multidimensional_difference_awareness_n120.csv",
        "top_n",
        False,
    ),
    (132, "SPAR - BBEH Mini - ISD", "bbeh_mini_n376.csv", "human_as_a_tool", True),
    (133, "SPAR - Long Bench V2 - Baseline", "longbenchv2_n392.csv", "none", False),
    (134, "SPAR - Long Safety - Baseline", "longsafety_n569.csv", "none", False),
    (135, "SPAR - BBEH Safety - Baseline", "bbeh_safety_n300.csv", "none", False),
    (136, "SPAR - BBEH Safety - ISD", "bbeh_safety_n300.csv", "human_as_a_tool", False),
    (236, "SPAR - BBEH Safety - Top-3", "bbeh_safety_n300.csv", "top_n", False),
    (237, "SPAR - Long Bench V2 - ISD", "longbenchv2_n392.csv", "human_as_a_tool", True),
    (238, "SPAR - Long Safety - ISD", "longsafety_n569.csv", "human_as_a_tool", True),
)


def test_the_recorded_collections_pass_every_check_against_production(
    client: TestClient, sync_engine
):
    """The real record, run over production's shape, gives exactly the record.

    Every listed experiment is assigned to its recorded card and wave — so no
    cross-check would refuse it on apply — and nothing else is touched.
    """
    with sync_engine.begin() as conn:
        for eid, name, filename, arm, archived in PRODUCTION:
            _insert_experiment(conn, eid, name, arm, filename)
            if archived:
                conn.execute(
                    text("UPDATE experiments SET archived_at = now() WHERE id = :id"),
                    {"id": eid},
                )

    result = _sync(apply=False)

    assigned = {
        a["experiment_id"]: (a["dataset_name"], a["arm"], a["wave"])
        for a in result["experiments_assigned"]
    }
    assert assigned == {eid: (c.card, c.arm, c.wave) for eid, c in COLLECTIONS.items()}
    unlisted = {eid for eid, *_ in PRODUCTION} - set(COLLECTIONS)
    assert _skips(result) == {eid: "not_in_manifest" for eid in unlisted}
    assert unlisted == {eid for eid, *_, archived in PRODUCTION if archived}
    assert result["manifest_missing"] == []
    assert len(result["groups_created"]) == 13
    assert sorted(result["collected_outside_schedule"]) == [
        "longbenchv2 sp26",
        "longsafety sp26",
        "safeagentbench_abstracted sp26",
    ]
    assert "safeagentbench_abstracted" in result["datasets_created"]
