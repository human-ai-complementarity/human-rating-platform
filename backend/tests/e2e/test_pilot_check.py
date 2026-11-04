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


def _mock_create_study() -> respx.Route:
    respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(
        return_value=httpx.Response(200, json={"id": "pg-1", "name": "pg", "project_id": "p"})
    )
    return respx.post(f"{PROLIFIC_BASE}/studies/").mock(
        return_value=httpx.Response(200, json={"id": "study-pc-1", "status": "UNPUBLISHED"})
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


def _run_check(client: TestClient, experiment_id: int, timeout: float = 20.0) -> dict:
    started = client.post(f"/api/admin/experiments/{experiment_id}/pilot-check")
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "running"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/admin/experiments/{experiment_id}/pilot-check").json()
        if run["status"] != "running":
            return run
        time.sleep(0.1)
    raise AssertionError("pilot check did not finish in time")


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
    assert client.get(f"/api/admin/experiments/{exp['id']}/pilot-check").status_code == 404


def test_a_control_arm_with_a_consent_source_passes(client: TestClient, terms_source):
    exp = _ready_experiment(client)
    assert exp["pilot_check"]["state"] == "not_run"

    run = _run_check(client, exp["id"])
    assert run["status"] == "passed", run["findings"]
    assert run["requested_by"] == "dev@local"
    assert run["assistance_steps"] is None
    # A control arm has no assistance stages; the judge has (test) keys, so the
    # judged checks that apply are stages of their own.
    assert run["stages"] == [
        "sampling",
        "checks",
        "instructions_coherent",
        "question_coherent",
        "done",
    ]
    assert run["stage"] == "done" and run["stage_detail"] is None
    assert run["stale"] is False
    assert sorted(run["sample_question_ids"]) == run["sample_question_ids"]
    assert len(run["sample_question_ids"]) == 3
    assert run["terms_ref"].startswith("standard v1:")

    grouped = _by_check(run)
    assert grouped["consent_configured"][0]["severity"] == "info"
    assert "standard v1" in grouped["consent_configured"][0]["message"]
    # The judge is "available" (test keys) but every call is stubbed to fail,
    # so judged checks skip with an info line rather than failing the run.
    for name in ("instructions_coherent", "question_coherent"):
        assert grouped[name][0]["severity"] == "info"
        assert "judge call failed" in grouped[name][0]["message"]
    assert grouped["assistance_output"][0]["message"].startswith("Skipped")

    refreshed = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert refreshed["pilot_check"]["state"] == "passed"
    assert refreshed["pilot_check"]["run_id"] == run["id"]
    assert refreshed["pilot_check"]["needs_acknowledgement"] is False

    detail = client.get(f"/api/admin/pilot-checks/{run['id']}").json()
    assert detail["id"] == run["id"]


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
    assert run["stages"] == [
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
    assert {f["question_id"] for f in grouped["assistance_output"]} == set(
        run["sample_question_ids"]
    )


def test_editing_the_experiment_makes_the_run_stale(client: TestClient, terms_source):
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
    # A run row that is still "running": the process owning it may be alive.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pilot_check_runs (experiment_id, status, fingerprint, config_sha256)"
                " VALUES (:eid, 'running', 'fp', 'cfg')"
            ),
            {"eid": exp["id"]},
        )
    resp = client.post(f"/api/admin/experiments/{exp['id']}/pilot-check")
    assert resp.status_code == 409
    assert client.get(f"/api/admin/experiments/{exp['id']}").json()["pilot_check"]["state"] == (
        "running"
    )


def test_a_run_left_running_past_its_deadline_is_aborted_and_replaced(
    client: TestClient, sync_engine, terms_source
):
    exp = _ready_experiment(client)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pilot_check_runs"
                " (experiment_id, status, fingerprint, config_sha256, created_at)"
                " VALUES (:eid, 'running', 'fp', 'cfg', NOW() - INTERVAL '2 days')"
            ),
            {"eid": exp["id"]},
        )
    assert client.get(f"/api/admin/experiments/{exp['id']}").json()["pilot_check"]["state"] == (
        "aborted"
    )
    run = _run_check(client, exp["id"])
    assert run["status"] == "passed"


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
    client: TestClient, enable_prolific, terms_source, pilot_check_gate
):
    _mock_create_study()
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
    _mock_create_study()
    exp = _ready_experiment(client)
    url = f"/api/admin/experiments/{exp['id']}/prolific/pilot"
    assert client.post(url, json=_PILOT).status_code == 409
    created = client.post(url, json={**_PILOT, "acknowledge_pilot_check": True})
    assert created.status_code == 200, created.text
    assert created.json()["pilot_check_run_id"] is None
    assert created.json()["pilot_check_acknowledged_by"] == "dev@local"


@respx.mock
def test_block_mode_has_no_acknowledgement_path(
    client: TestClient, enable_prolific, pilot_check_gate
):
    _mock_create_study()
    pilot_check_gate.gate.mode = "block"
    exp = _ready_experiment(client)
    url = f"/api/admin/experiments/{exp['id']}/prolific/pilot"
    refused = client.post(url, json={**_PILOT, "acknowledge_pilot_check": True})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "pilot_check_blocked"


@respx.mock
def test_the_one_click_launch_passes_the_acknowledgement_through(
    client: TestClient, enable_prolific, pilot_check_gate
):
    _mock_create_study()
    dataset = client.post(
        "/api/admin/datasets",
        json={
            "name": "gated",
            "waves": ["fall25"],
            "external_study_name": "Passage rating",
            "internal_study_name": "{dataset} {wave} {method}",
            "study_blurb": "Rate short passages.",
            "estimated_completion_time": 10,
            "reward": 500,
        },
    ).json()
    group = client.post(
        "/api/admin/experiment-groups",
        json={"name": "gated fall25", "dataset_id": dataset["id"], "wave": "fall25"},
    ).json()
    exp = client.post("/api/admin/experiments", json={"group_id": group["id"]}).json()
    client.post(
        f"/api/admin/experiments/{exp['id']}/upload",
        files={
            "file": (
                "q.csv",
                "question_id,question_text,gt_answer,options,question_type\n"
                "q1,Is this useful?,Yes,Yes|No,MC\n",
                "text/csv",
            )
        },
    )
    client.patch(
        f"/api/admin/experiments/{exp['id']}",
        json={"assistance_method": "none", "description": "Instructions."},
    )
    url = f"/api/admin/experiments/{exp['id']}/launch"
    assert client.post(url, json={}).status_code == 409
    created = client.post(url, json={"acknowledge_pilot_check": True})
    assert created.status_code == 200, created.text
    assert created.json()["pilot_check_acknowledged_by"] == "dev@local"
