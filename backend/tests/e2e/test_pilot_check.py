"""The pilot check end to end (docs/pilot-check.md): a run starts in the
background and lands on a status, the experiment read summarises it, and the
launch gate refuses a study until the check is clean or acknowledged.

Every provider call is stubbed to fail (conftest), which is itself a good
fixture: an assisted arm's run must report that, not crash.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import text

from config import get_settings
from models import Experiment

PROLIFIC_BASE = "https://api.prolific.com/api/v1"

_PILOT = {
    "description": "Test study",
    "estimated_completion_time": 10,
    "reward": 500,
    "pilot_places": 5,
    "device_compatibility": ["desktop"],
}


@pytest.fixture
def enable_prolific():
    settings = get_settings()
    original = (settings.prolific.api_token, settings.prolific.project_id)
    settings.prolific.api_token = "test-token"
    settings.prolific.project_id = "test-project"
    try:
        yield settings
    finally:
        settings.prolific.api_token, settings.prolific.project_id = original


def _mock_prolific_study(study_id: str = "study-pc-1") -> respx.Route:
    respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(
        return_value=httpx.Response(200, json={"id": "pg-1", "name": "pg", "project_id": "p"})
    )
    respx.get(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=httpx.Response(200, json={"id": study_id, "status": "UNPUBLISHED"})
    )
    respx.patch(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=httpx.Response(200, json={"id": study_id, "status": "UNPUBLISHED"})
    )
    respx.post(f"{PROLIFIC_BASE}/studies/{study_id}/transition/").mock(
        return_value=httpx.Response(200, json={"id": study_id, "status": "ACTIVE"})
    )
    return respx.post(f"{PROLIFIC_BASE}/studies/").mock(
        return_value=httpx.Response(200, json={"id": study_id, "status": "UNPUBLISHED"})
    )


def _ready_experiment(client: TestClient, method: str = "none", **params) -> dict:
    """An experiment past the hard launch gate: questions, instructions, a name."""
    exp = client.post("/api/admin/experiments", json={"name": "Checked"}).json()
    resp = client.post(
        f"/api/admin/experiments/{exp['id']}/upload",
        files={
            "file": (
                "q.csv",
                "question_id,question_text,gt_answer,options,question_type\n"
                "q1,Is this useful?,Yes,Yes|No,MC\n"
                "q2,Is this clear?,Yes,Yes|No,MC\n"
                "q3,Summarise the passage.,,,free_text\n",
                "text/csv",
            )
        },
    )
    assert resp.status_code == 200, resp.text
    payload = {
        "assistance_method": method,
        "internal_name": "checked internal",
        "description": "Answer using only the passage.",
    }
    if params:
        payload["assistance_params"] = params
    resp = client.patch(f"/api/admin/experiments/{exp['id']}", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _wait_for_run(client: TestClient, experiment_id: int, timeout: float = 20.0) -> dict:
    """The latest run once it is no longer running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/admin/experiments/{experiment_id}/pilot-check").json()
        if run["status"] != "running":
            return run
        time.sleep(0.1)
    raise AssertionError("pilot check did not finish in time")


def _run_check(client: TestClient, experiment_id: int, timeout: float = 20.0) -> dict:
    started = client.post(f"/api/admin/experiments/{experiment_id}/pilot-check")
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "running"
    return _wait_for_run(client, experiment_id, timeout)


def _upload(client: TestClient, experiment_id: int, rows: str) -> None:
    resp = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={
            "file": (
                "q.csv",
                "question_id,question_text,gt_answer,options,question_type\n" + rows,
                "text/csv",
            )
        },
    )
    assert resp.status_code == 200, resp.text


def _by_check(run: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for finding in run["findings"]:
        grouped.setdefault(finding["check"], []).append(finding)
    return grouped


def test_an_experiment_without_questions_cannot_be_checked(client: TestClient):
    exp = client.post("/api/admin/experiments", json={"name": "Empty"}).json()
    resp = client.post(f"/api/admin/experiments/{exp['id']}/pilot-check")
    assert resp.status_code == 400
    assert "Upload questions" in resp.json()["detail"]
    latest = client.get(f"/api/admin/experiments/{exp['id']}/pilot-check")
    assert (latest.status_code, latest.json()) == (200, None)


def test_a_control_arm_with_a_consent_source_and_no_judge_passes(
    client: TestClient, terms_source, no_judge
):
    exp = _ready_experiment(client)
    run = _run_check(client, exp["id"])
    assert run["status"] == "passed", run["findings"]
    assert [stage["id"] for stage in run["stages"]] == ["sampling", "checks", "done"]
    grouped = _by_check(run)
    for name in ("instructions_coherent", "question_coherent"):
        assert grouped[name][0]["severity"] == "info"
        assert "no API key" in grouped[name][0]["message"]
    assert client.get(f"/api/admin/experiments/{exp['id']}").json()["pilot_check"]["state"] == (
        "passed"
    )


def test_a_control_arm_whose_judge_calls_fail_lands_with_warnings(client: TestClient, terms_source):
    exp = _ready_experiment(client)
    assert exp["pilot_check"]["state"] == "not_run"

    run = _run_check(client, exp["id"])
    assert run["status"] == "warnings", run["findings"]
    assert run["requested_by"] == "dev@local"
    assert run["assistance_steps"] is None
    # A control arm has no assistance stages; the judge has (test) keys, so the
    # judged checks that apply are stages of their own.
    assert [stage["id"] for stage in run["stages"]] == [
        "sampling",
        "checks",
        "instructions_coherent",
        "question_coherent",
        "done",
    ]
    assert run["stages"][2]["label"] == "Review instructions"
    assert run["stage"] == "done" and run["stage_detail"] is None
    assert run["stale"] is False
    assert sorted(run["sample_question_ids"]) == run["sample_question_ids"]
    assert len(run["sample_question_ids"]) == 3
    assert run["terms_ref"].startswith("standard v1:")

    grouped = _by_check(run)
    assert grouped["consent_configured"][0]["severity"] == "info"
    assert grouped["consent_configured"][0]["check_label"] == "Consent statement"
    assert "standard v1" in grouped["consent_configured"][0]["message"]
    # The judge is "available" (test keys) but every call is stubbed to fail.
    # A judge that never answered is a warning, not a pass: the admin is asked
    # before launching on it.
    for name in ("instructions_coherent", "question_coherent"):
        assert grouped[name][0]["severity"] == "warning"
        assert "judge call failed" in grouped[name][0]["message"]
    assert grouped["assistance_output"][0]["message"].startswith("Skipped")

    refreshed = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert refreshed["pilot_check"]["state"] == "warnings"
    assert refreshed["pilot_check"]["run_id"] == run["id"]
    assert refreshed["pilot_check"]["needs_acknowledgement"] is False


def test_the_placeholder_consent_fails_the_run(client: TestClient):
    assert get_settings().terms.source_url == ""
    exp = _ready_experiment(client)
    run = _run_check(client, exp["id"])
    assert run["status"] == "failed"
    consent = _by_check(run)["consent_configured"]
    assert consent[0]["severity"] == "error"
    assert "TERMS__SOURCE_URL" in consent[0]["message"]
    summary = client.get(f"/api/admin/experiments/{exp['id']}").json()["pilot_check"]
    assert summary["state"] == "failed"
    assert summary["counts"]["error"] == 1
    assert summary["needs_acknowledgement"] is False  # the suite runs with the gate off


def test_an_assisted_arm_whose_provider_fails_is_reported(client: TestClient, terms_source):
    exp = _ready_experiment(
        client,
        "top_n",
        assistance_models={
            "top_n": {
                "model": "openrouter/anthropic/claude-sonnet-4-6",
                "reasoning_effort": None,
                "text_verbosity": None,
                "temperature": 0,
            }
        },
    )
    run = _run_check(client, exp["id"])
    assert run["status"] == "failed"
    assert [stage["id"] for stage in run["stages"]] == [
        "sampling",
        "assistance",
        "checks",
        "instructions_coherent",
        "question_coherent",
        "assistance_useful",
        "done",
    ]
    assert run["stage"] == "done"
    assert len(run["assistance_steps"]) == 3
    assert all(step["step_type"] == "none" for step in run["assistance_steps"])
    grouped = _by_check(run)
    assert grouped["assistance_health"][0]["severity"] == "error"
    assert "3 of 3" in grouped["assistance_health"][0]["message"]
    assert {qid for f in grouped["assistance_output"] for qid in f["question_ids"]} == set(
        run["sample_question_ids"]
    )


def test_editing_the_experiment_makes_the_run_stale(client: TestClient, terms_source, no_judge):
    exp = _ready_experiment(client)
    run = _run_check(client, exp["id"])
    assert run["status"] == "passed"
    patched = client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={"assistance_method": "none", "description": "New instructions."},
    ).json()
    assert patched["pilot_check"]["state"] == "stale"
    assert client.get(f"/api/admin/experiments/{exp['id']}/pilot-check").json()["stale"] is True
    rerun = _run_check(client, exp["id"])
    assert rerun["id"] != run["id"]
    assert client.get(f"/api/admin/experiments/{exp['id']}").json()["pilot_check"]["state"] == (
        "passed"
    )


def test_a_second_run_cannot_start_while_one_is_running(client: TestClient, sync_engine):
    exp = _ready_experiment(client)
    # A run row still "running" and recently touched: its process is alive.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pilot_check_runs"
                " (experiment_id, status, fingerprint, config_sha256, updated_at)"
                " VALUES (:eid, 'running', 'fp', 'cfg', NOW())"
            ),
            {"eid": exp["id"]},
        )
    resp = client.post(f"/api/admin/experiments/{exp['id']}/pilot-check")
    assert resp.status_code == 409
    assert client.get(f"/api/admin/experiments/{exp['id']}").json()["pilot_check"]["state"] == (
        "running"
    )


def test_a_run_that_stopped_reporting_progress_is_aborted_and_replaced(
    client: TestClient, sync_engine, terms_source, no_judge
):
    exp = _ready_experiment(client)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pilot_check_runs"
                " (experiment_id, status, fingerprint, config_sha256, created_at, updated_at)"
                " VALUES (:eid, 'running', 'fp', 'cfg', NOW() - INTERVAL '1 hour',"
                " NOW() - INTERVAL '1 hour')"
            ),
            {"eid": exp["id"]},
        )
    assert client.get(f"/api/admin/experiments/{exp['id']}").json()["pilot_check"]["state"] == (
        "aborted"
    )
    latest = client.get(f"/api/admin/experiments/{exp['id']}/pilot-check").json()
    assert latest["status"] == "aborted"
    assert "stopped reporting progress" in latest["error"]
    run = _run_check(client, exp["id"])
    assert run["status"] == "passed"


@respx.mock
def test_publishing_the_pilot_keeps_the_run_fresh(
    client: TestClient, enable_prolific, terms_source
):
    """The first publish pins the consent statement on the experiment; that
    must not stale a run made on the draft, or every later round would nag."""
    _mock_prolific_study()
    exp = _ready_experiment(client)
    run = _run_check(client, exp["id"])
    assert run["stale"] is False

    url = f"/api/admin/experiments/{exp['id']}/prolific"
    created = client.post(f"{url}/pilot", json=_PILOT)
    assert created.status_code == 200, created.text
    published = client.post(f"{url}/rounds/{created.json()['id']}/publish")
    assert published.status_code == 200, published.text

    experiment = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert experiment["consent_statement_ref"], "the publish should have pinned the terms"
    assert experiment["pilot_check"]["state"] == run["status"]
    assert client.get(f"/api/admin/experiments/{exp['id']}/pilot-check").json()["stale"] is False


def test_the_list_flags_a_checked_in_experiment_without_a_run(client: TestClient, pilot_check_gate):
    exp = _ready_experiment(client)
    listed = next(e for e in client.get("/api/admin/experiments").json() if e["id"] == exp["id"])
    assert listed["needs_attention"] is True
    assert listed["attention_reason"] == "Run the pilot check before launching."
    assert listed["pilot_check"]["needs_acknowledgement"] is True
    bare = client.post("/api/admin/experiments", json={"name": "No questions"}).json()
    listed = next(e for e in client.get("/api/admin/experiments").json() if e["id"] == bare["id"])
    assert listed["needs_attention"] is False


@respx.mock
def test_the_gate_refuses_a_pilot_until_the_check_is_clean_or_acknowledged(
    client: TestClient, enable_prolific, terms_source, pilot_check_gate, no_judge
):
    _mock_prolific_study()
    exp = _ready_experiment(client)
    url = f"/api/admin/experiments/{exp['id']}/prolific/pilot"

    refused = client.post(url, json=_PILOT)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "pilot_check"
    assert detail["pilot_check"]["state"] == "not_run"
    assert "has not been run" in detail["message"]

    run = _run_check(client, exp["id"])
    assert run["status"] == "passed"
    created = client.post(url, json=_PILOT)
    assert created.status_code == 200, created.text
    assert created.json()["pilot_check_run_id"] == run["id"]
    assert created.json()["pilot_check_acknowledged_by"] is None


@respx.mock
def test_launching_anyway_records_who_acknowledged(
    client: TestClient, enable_prolific, pilot_check_gate
):
    _mock_prolific_study()
    exp = _ready_experiment(client)
    url = f"/api/admin/experiments/{exp['id']}/prolific/pilot"
    assert client.post(url, json=_PILOT).status_code == 409
    created = client.post(url, json={**_PILOT, "acknowledge_pilot_check": True})
    assert created.status_code == 200, created.text
    assert created.json()["pilot_check_run_id"] is None
    assert created.json()["pilot_check_acknowledged_by"] == "dev@local"


@respx.mock
def test_a_flagged_run_is_acknowledged_once(
    client: TestClient, enable_prolific, terms_source, pilot_check_gate
):
    """Nothing changed between the pilot and the next round, so the same
    flagged run is not asked about twice; the acknowledger carries over."""
    _mock_prolific_study()
    exp = _ready_experiment(client)
    run = _run_check(client, exp["id"])
    assert run["status"] == "warnings"  # the judge calls fail (conftest)

    url = f"/api/admin/experiments/{exp['id']}/prolific"
    assert client.post(f"{url}/pilot", json=_PILOT).status_code == 409
    pilot = client.post(f"{url}/pilot", json={**_PILOT, "acknowledge_pilot_check": True})
    assert pilot.status_code == 200, pilot.text
    assert pilot.json()["pilot_check_acknowledged_by"] == "dev@local"

    assert client.post(f"{url}/rounds/{pilot.json()['id']}/publish").status_code == 200
    respx.post(f"{PROLIFIC_BASE}/studies/study-pc-1/transition/").mock(
        return_value=httpx.Response(200, json={"id": "study-pc-1", "status": "AWAITING_REVIEW"})
    )
    assert client.post(f"{url}/rounds/{pilot.json()['id']}/close").status_code == 200

    _mock_prolific_study("study-pc-2")
    second = client.post(f"{url}/rounds", json={"places": 4})
    assert second.status_code == 200, second.text
    assert second.json()["pilot_check_run_id"] == run["id"]
    assert second.json()["pilot_check_acknowledged_by"] == "dev@local"


def _run_count(sync_engine, experiment_id: int) -> int:
    with sync_engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM pilot_check_runs WHERE experiment_id = :eid"),
            {"eid": experiment_id},
        ).scalar_one()


def _edit(client: TestClient, experiment_id: int, **fields) -> dict:
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}", json={"assistance_method": "none", **fields}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_run_starts_on_its_own_once_the_experiment_is_ready(
    client: TestClient, sync_engine, terms_source, no_judge, pilot_check_autostart
):
    """With `auto_start` on (the suite turns it off), the upload or edit that
    leaves the experiment ready to launch starts the check, recorded against
    the admin who made it. The pipeline's upload carries the instructions, so
    there it is the upload; on the website it is usually the edit after.
    Edits a finished run already covers start nothing; a run still going is
    left alone. A testing preview cannot launch but is still checked."""
    exp = client.post(
        "/api/admin/experiments", json={"name": "Auto", "tags": ["preview-testing"]}
    ).json()
    _upload(client, exp["id"], "q1,Is this useful?,Yes,Yes|No,MC\n")
    # Questions alone are not launchable: no rater instructions yet.
    assert client.get(f"/api/admin/experiments/{exp['id']}/pilot-check").json() is None

    _edit(client, exp["id"], description="Answer using only the passage.", internal_name="auto")
    run = _wait_for_run(client, exp["id"])
    assert run["requested_by"] == "dev@local"
    assert run["status"] == "passed", run["findings"]
    assert run["stale"] is False
    assert run["sample_question_ids"] and len(run["sample_question_ids"]) == 1

    # An edit the run does not look at (the public name) changes nothing.
    _edit(client, exp["id"], name="Renamed")
    assert _run_count(sync_engine, exp["id"]) == 1

    # More questions: the finished run no longer covers the experiment, so the
    # upload starts another.
    _upload(client, exp["id"], "q2,Is this clear?,Yes,Yes|No,MC\n")
    rerun = _wait_for_run(client, exp["id"])
    assert rerun["id"] != run["id"]
    assert len(rerun["sample_question_ids"]) == 2

    # A run still going (its row recently touched) is left to finish.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pilot_check_runs"
                " (experiment_id, status, fingerprint, config_sha256, updated_at)"
                " VALUES (:eid, 'running', 'fp', 'cfg', NOW())"
            ),
            {"eid": exp["id"]},
        )
    _upload(client, exp["id"], "q3,Summarise the passage.,,,free_text\n")
    latest = client.get(f"/api/admin/experiments/{exp['id']}/pilot-check").json()
    assert latest["status"] == "running" and latest["fingerprint"] == "fp"
    assert _run_count(sync_engine, exp["id"]) == 3


def test_an_edit_that_lands_mid_run_gets_a_follow_up_run(
    client: TestClient, sync_engine, monkeypatch, terms_source, no_judge, pilot_check_autostart
):
    """An edit while a run is going is skipped (one run at a time), so the
    run that finishes is stale. Finishing then starts one more, which covers
    the edit; that one is fresh, so it starts no third."""
    from services.pilot_check import runs

    real_perform = runs._perform
    edited = False
    experiment_id: dict[str, int] = {}

    async def perform_after_an_edit(run_id, database, settings):
        nonlocal edited
        if not edited:
            edited = True
            async with database.session() as db:
                experiment = await db.get(Experiment, experiment_id["value"])
                experiment.description = "Changed while the run was going."
                await db.commit()
        await real_perform(run_id, database, settings)

    monkeypatch.setattr(runs, "_perform", perform_after_an_edit)
    exp = client.post("/api/admin/experiments", json={"name": "Auto"}).json()
    experiment_id["value"] = exp["id"]
    _upload(client, exp["id"], "q1,Is this useful?,Yes,Yes|No,MC\n")
    _edit(client, exp["id"], description="Answer using only the passage.", internal_name="auto")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _run_count(sync_engine, exp["id"]) == 2:
            latest = client.get(f"/api/admin/experiments/{exp['id']}/pilot-check").json()
            if latest["status"] != "running":
                break
        time.sleep(0.1)
    else:
        raise AssertionError("the follow-up run did not finish in time")
    assert latest["stale"] is False
    with sync_engine.connect() as conn:
        first_fingerprint = (
            conn.execute(
                text(
                    "SELECT fingerprint FROM pilot_check_runs WHERE experiment_id = :eid ORDER BY id"
                ),
                {"eid": exp["id"]},
            )
            .scalars()
            .first()
        )
    assert first_fingerprint != latest["fingerprint"]
    time.sleep(0.5)  # a third run would have started by now
    assert _run_count(sync_engine, exp["id"]) == 2
