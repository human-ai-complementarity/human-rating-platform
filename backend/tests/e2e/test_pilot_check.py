"""The pilot check end to end (docs/pilot-check.md): a run starts in the
background and lands on a status, and the experiment read summarises it.

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
    assert [stage["id"] for stage in run["stages"]] == ["sampling", "checks", "done"]
    assert run["stages"][1]["label"] == "Check rendering"
    assert run["stage"] == "done" and run["stage_detail"] is None
    assert run["stale"] is False
    assert sorted(run["sample_question_ids"]) == run["sample_question_ids"]
    assert len(run["sample_question_ids"]) == 3
    assert run["terms_ref"].startswith("standard v1:")

    grouped = _by_check(run)
    assert grouped["consent_configured"][0]["severity"] == "info"
    assert grouped["consent_configured"][0]["check_label"] == "Consent statement"
    assert "standard v1" in grouped["consent_configured"][0]["message"]

    refreshed = client.get(f"/api/admin/experiments/{exp['id']}").json()
    assert refreshed["pilot_check"]["state"] == "passed"
    assert refreshed["pilot_check"]["run_id"] == run["id"]


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
    assert [stage["id"] for stage in run["stages"]] == ["sampling", "assistance", "checks", "done"]
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
    client: TestClient, sync_engine, terms_source
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
