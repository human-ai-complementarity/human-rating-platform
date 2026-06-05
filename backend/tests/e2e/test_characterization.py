from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import ExperimentRound

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _create_experiment(client: TestClient, *, completion_url: str | None = None) -> dict:
    response = client.post(
        "/api/admin/experiments",
        json={
            "name": _unique_name("experiment"),
            "num_ratings_per_question": 2,
            "prolific_completion_url": completion_url,
        },
    )
    assert response.status_code == 200
    return response.json()


def _upload_questions(client: TestClient, experiment_id: int) -> None:
    csv_data = (
        "question_id,question_text,gt_answer,options,question_type\n"
        "q1,Is this useful?,Yes,Yes|No,MC\n"
        "q2,Explain why,,,"
    )
    response = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("questions.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 200


def _start_session(client: TestClient, experiment_id: int, prolific_pid: str = "PID_A") -> dict:
    response = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment_id,
            "PROLIFIC_PID": prolific_pid,
            "STUDY_ID": "STUDY_1",
            "SESSION_ID": f"SESSION_{prolific_pid}",
        },
    )
    assert response.status_code == 200
    return response.json()


def _rater_headers(session_payload: dict) -> dict[str, str]:
    return {"X-Rater-Session": session_payload["rater_session_token"]}


def _seed_export_dataset(sync_engine, experiment_id: int, row_count: int) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO questions (
                    experiment_id,
                    question_id,
                    question_text,
                    gt_answer,
                    options,
                    question_type,
                    extra_data
                )
                SELECT
                    :experiment_id,
                    CONCAT('bulk-', gs::text),
                    CONCAT('Bulk question ', gs::text),
                    '',
                    '',
                    'MC',
                    '{}'
                FROM generate_series(1, :row_count) AS gs
                """
            ),
            {"experiment_id": experiment_id, "row_count": row_count},
        )

        rater_id = conn.execute(
            text(
                """
                INSERT INTO raters (
                    prolific_id,
                    study_id,
                    session_id,
                    experiment_id,
                    session_start,
                    is_active
                )
                VALUES (
                    'PID_EXPORT',
                    'STUDY_EXPORT',
                    'SESSION_EXPORT',
                    :experiment_id,
                    NOW(),
                    true
                )
                RETURNING id
                """
            ),
            {"experiment_id": experiment_id},
        ).scalar_one()

        conn.execute(
            text(
                """
                INSERT INTO ratings (
                    question_id,
                    rater_id,
                    answer,
                    confidence,
                    time_started,
                    time_submitted
                )
                SELECT
                    q.id,
                    :rater_id,
                    'Yes',
                    3,
                    NOW(),
                    NOW()
                FROM questions q
                WHERE q.experiment_id = :experiment_id
                """
            ),
            {"experiment_id": experiment_id, "rater_id": rater_id},
        )


def test_health_endpoint_smoke(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert "version" in body
    assert "commit" in body


def test_list_experiments_returns_empty_list_initially(client: TestClient):
    response = client.get("/api/admin/experiments")
    assert response.status_code == 200
    assert response.json() == []


def test_create_experiment_then_list_contains_it(client: TestClient):
    created = _create_experiment(client)

    response = client.get("/api/admin/experiments")
    assert response.status_code == 200
    items = response.json()

    assert len(items) == 1
    assert items[0]["id"] == created["id"]
    assert items[0]["question_count"] == 0
    assert items[0]["rating_count"] == 0


def test_upload_questions_records_upload_and_stats(client: TestClient):
    experiment = _create_experiment(client)
    experiment_id = experiment["id"]

    _upload_questions(client, experiment_id)

    uploads_response = client.get(f"/api/admin/experiments/{experiment_id}/uploads")
    stats_response = client.get(f"/api/admin/experiments/{experiment_id}/stats")

    assert uploads_response.status_code == 200
    assert len(uploads_response.json()) == 1
    assert uploads_response.json()[0]["question_count"] == 2

    assert stats_response.status_code == 200
    stats_payload = stats_response.json()
    assert stats_payload["total_questions"] == 2
    assert stats_payload["total_ratings"] == 0


def test_upload_rejects_non_csv_file(client: TestClient):
    experiment = _create_experiment(client)

    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("questions.txt", "nope", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "File must be a CSV file"


def test_upload_accepts_large_question_text_fields(client: TestClient):
    experiment = _create_experiment(client)
    large_question_text = "x" * 200_000

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["question_id", "question_text", "gt_answer", "options", "question_type"])
    writer.writerow(["long-q", large_question_text, "Yes", "Yes|No", "MC"])

    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("long_questions.csv", output.getvalue(), "text/csv")},
    )

    assert response.status_code == 200
    assert response.json()["message"] == "Uploaded 1 questions"

    stats_response = client.get(f"/api/admin/experiments/{experiment['id']}/stats")
    assert stats_response.status_code == 200
    assert stats_response.json()["total_questions"] == 1


def test_start_session_creates_new_rater_session(client: TestClient):
    experiment = _create_experiment(
        client,
        completion_url="https://app.prolific.com/submissions/complete?cc=ABCD1234",
    )

    payload = _start_session(client, experiment["id"], prolific_pid="PID_1")

    assert payload["rater_id"] > 0
    assert payload["experiment_name"] == experiment["name"]
    assert payload["completion_url"].startswith("https://app.prolific.com/")


def test_start_session_twice_resumes_same_active_session(client: TestClient):
    experiment = _create_experiment(client)

    first = _start_session(client, experiment["id"], prolific_pid="PID_RESUME")
    second = _start_session(client, experiment["id"], prolific_pid="PID_RESUME")

    assert first["rater_id"] == second["rater_id"]
    assert first["session_start"] == second["session_start"]


def test_start_session_rejects_after_end_session(client: TestClient):
    experiment = _create_experiment(client)
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_DONE")

    end_response = client.post(
        "/api/raters/end-session",
        headers=_rater_headers(session_payload),
    )
    restart_response = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment["id"],
            "PROLIFIC_PID": "PID_DONE",
            "STUDY_ID": "STUDY_1",
            "SESSION_ID": "SESSION_PID_DONE_RESTART",
        },
    )

    assert end_response.status_code == 200
    assert restart_response.status_code == 403


def test_next_question_returns_eligible_question(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_NEXT")

    response = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["question_id"] in {"q1", "q2"}


def test_submit_rating_success_then_duplicate_rejected(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_SUBMIT")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    submit_payload = {
        "question_id": question["id"],
        "answer": "Yes",
        "confidence": 4,
        "time_started": datetime.now(UTC).isoformat(),
    }

    first = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json=submit_payload,
    )
    duplicate = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json=submit_payload,
    )

    assert first.status_code == 200
    assert first.json()["success"] is True
    assert duplicate.status_code == 400


def test_submit_rating_rejects_invalid_confidence(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_CONF")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    response = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 9,
            "time_started": datetime.now(UTC).isoformat(),
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(error["loc"][-1] == "confidence" for error in detail)


def test_session_status_reflects_completed_questions(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_STATUS")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()
    client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "No",
            "confidence": 3,
            "time_started": datetime.now(UTC).isoformat(),
        },
    )

    response = client.get(
        "/api/raters/session-status",
        headers=_rater_headers(session_payload),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["questions_completed"] == 1
    assert payload["time_remaining_seconds"] > 0


def test_next_question_marks_expired_session_inactive(
    client: TestClient,
    backdate_rater_session,
):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_EXPIRED")

    backdate_rater_session(session_payload["rater_id"])

    expired_response = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    )
    status_response = client.get(
        "/api/raters/session-status",
        headers=_rater_headers(session_payload),
    )

    assert expired_response.status_code == 403
    assert expired_response.json()["detail"] == "Session expired"
    assert status_response.status_code == 200
    assert status_response.json()["is_active"] is False


def test_export_ratings_streams_large_dataset_in_chunks(client: TestClient, sync_engine):
    settings = get_settings()
    row_count = settings.testing.export_seed_row_count
    experiment = _create_experiment(client)
    _seed_export_dataset(sync_engine, experiment["id"], row_count=row_count)

    with client.stream("GET", f"/api/admin/experiments/{experiment['id']}/export") as response:
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        chunks = list(response.iter_text())

    assert len(chunks) >= 1

    parsed_rows = list(csv.reader(io.StringIO("".join(chunks))))
    assert parsed_rows[0][0] == "rating_id"
    assert len(parsed_rows) == row_count + 1


def test_analytics_endpoint_returns_expected_payload_shape(client: TestClient):
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_ANALYTICS")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    submit_response = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": datetime.now(UTC).isoformat(),
        },
    )
    assert submit_response.status_code == 200

    analytics_response = client.get(f"/api/admin/experiments/{experiment['id']}/analytics")
    assert analytics_response.status_code == 200

    payload = analytics_response.json()
    overview = payload["overview"]
    assert payload["experiment_name"] == experiment["name"]
    assert overview["total_questions"] == 2
    assert overview["total_ratings"] == 1
    assert overview["total_raters"] == 1
    assert isinstance(payload["questions"], list) and len(payload["questions"]) == 1
    assert isinstance(payload["raters"], list) and len(payload["raters"]) == 1
    assert payload["questions"][0]["answer_distribution"] == {"Yes": 1}


def test_migration_runner_current_and_history_commands_succeed():
    revision_pattern = re.compile(r'^revision:\s*str\s*=\s*"([^"]+)"', re.MULTILINE)
    down_pattern = re.compile(r"^down_revision:\s*.*=\s*(.+)$", re.MULTILINE)
    revisions: set[str] = set()
    down_revisions: set[str] = set()

    for path in (BACKEND_DIR / "alembic" / "versions").glob("*.py"):
        if path.name == "__init__.py":
            continue
        content = path.read_text()
        revision_match = revision_pattern.search(content)
        assert revision_match is not None
        revisions.add(revision_match.group(1))

        down_match = down_pattern.search(content)
        assert down_match is not None
        down_raw = down_match.group(1).strip()
        if down_raw == "None":
            continue
        for revision in re.findall(r'"([^"]+)"', down_raw):
            down_revisions.add(revision)

    assert revisions
    head_revisions = sorted(revisions - down_revisions)
    assert head_revisions

    current = subprocess.run(
        ["sh", "scripts/migrate.sh", "current"],
        cwd=BACKEND_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    history = subprocess.run(
        ["sh", "scripts/migrate.sh", "history"],
        cwd=BACKEND_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert current.returncode == 0
    assert history.returncode == 0

    current_output = f"{current.stdout}\n{current.stderr}"
    history_output = f"{history.stdout}\n{history.stderr}"
    assert "(head)" in current_output
    assert any(rev in current_output for rev in head_revisions)
    for revision_id in revisions:
        assert revision_id in history_output


def test_app_creation_succeeds_with_default_env():
    env = os.environ.copy()
    env.setdefault("APP_SECRET_KEY", "test-secret")

    result = subprocess.run(
        [sys.executable, "-c", "from main import create_app; create_app(); print('ok')"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "ok" in result.stdout


# ── Prolific integration tests ──────────────────────────────────────────────
# These use respx to mock outbound httpx calls to the Prolific API, verifying
# the full path: TestClient → FastAPI → service → Prolific client → mock.

PROLIFIC_BASE = "https://api.prolific.com/api/v1"
PROLIFIC_STUDY_ID = "65abc123def456"


@pytest.fixture()
def enable_prolific():
    """Temporarily enable Prolific by setting an API token on the cached settings."""
    settings = get_settings()
    original = settings.prolific.api_token
    settings.prolific.api_token = "test-token"
    yield settings
    settings.prolific.api_token = original


def _prolific_experiment_payload() -> dict:
    return {
        "name": _unique_name("prolific-exp"),
        "num_ratings_per_question": 2,
        "prolific_completion_url": None,
    }


def _pilot_payload() -> dict:
    return {
        "description": "Test study",
        "estimated_completion_time": 10,
        "reward": 500,
        "pilot_places": 5,
        "device_compatibility": ["desktop"],
    }


def _mock_create_study(*, status: int = 200, study_id: str = PROLIFIC_STUDY_ID) -> respx.Route:
    body = {"id": study_id, "status": "UNPUBLISHED"} if status == 200 else {}
    return respx.post(f"{PROLIFIC_BASE}/studies/").mock(return_value=Response(status, json=body))


def _mock_publish_study(*, study_id: str = PROLIFIC_STUDY_ID) -> respx.Route:
    return respx.post(f"{PROLIFIC_BASE}/studies/{study_id}/transition/").mock(
        return_value=Response(200, json={"id": study_id, "status": "ACTIVE"})
    )


def _mock_close_study(
    *,
    study_id: str = PROLIFIC_STUDY_ID,
    closed_status: str = "AWAITING_REVIEW",
) -> respx.Route:
    return respx.post(f"{PROLIFIC_BASE}/studies/{study_id}/transition/").mock(
        return_value=Response(200, json={"id": study_id, "status": closed_status})
    )


def _mock_delete_study(*, study_id: str = PROLIFIC_STUDY_ID, status: int = 204) -> respx.Route:
    body = {} if status == 204 else {"error": "fail"}
    return respx.delete(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=Response(status, json=body)
    )


def _mock_get_study(
    *,
    study_id: str = PROLIFIC_STUDY_ID,
    study_status: str = "ACTIVE",
    status: int = 200,
) -> respx.Route:
    body = {"id": study_id, "status": study_status} if status == 200 else {"error": "fail"}
    return respx.get(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=Response(status, json=body)
    )


def _mock_project(
    *,
    project_id: str,
    workspace_id: str | None = "WS_ABC",
    status: int = 200,
) -> respx.Route:
    if status == 200:
        body: dict = {"id": project_id}
        if workspace_id is not None:
            body["workspace"] = workspace_id
    else:
        body = {}
    return respx.get(f"{PROLIFIC_BASE}/projects/{project_id}/").mock(
        return_value=Response(status, json=body)
    )


def _mock_workspace_balance(
    *,
    workspace_id: str,
    currency_code: str = "USD",
    status: int = 200,
) -> respx.Route:
    body = (
        {
            "currency_code": currency_code,
            "total_balance": 0,
            "available_balance": 0,
        }
        if status == 200
        else {}
    )
    return respx.get(f"{PROLIFIC_BASE}/workspaces/{workspace_id}/balance/").mock(
        return_value=Response(status, json=body)
    )


def _mock_update_study(
    *,
    study_id: str = PROLIFIC_STUDY_ID,
    status: int = 200,
) -> respx.Route:
    body = {"id": study_id, "status": "UNPUBLISHED"} if status == 200 else {"error": "fail"}
    return respx.patch(f"{PROLIFIC_BASE}/studies/{study_id}/").mock(
        return_value=Response(status, json=body)
    )


@pytest.fixture(autouse=True)
def _reset_prolific_currency_cache():
    # Module-level cache in services.admin.prolific persists across tests in
    # the same process; reset it so each test sees a clean lookup state.
    from services.admin.prolific import _reset_currency_cache

    _reset_currency_cache()
    yield
    _reset_currency_cache()


def _patch_commit_to_fail_for_round(
    monkeypatch: pytest.MonkeyPatch,
    *,
    round_number: int,
) -> None:
    original_commit = AsyncSession.commit
    state = {"failed": False}

    async def failing_commit(self: AsyncSession, *args, **kwargs):
        pending_rounds = [
            obj
            for obj in self.sync_session.new
            if isinstance(obj, ExperimentRound) and obj.round_number == round_number
        ]
        if pending_rounds and not state["failed"]:
            state["failed"] = True
            raise IntegrityError(
                "forced experiment_round conflict",
                params=None,
                orig=Exception("forced experiment_round conflict"),
            )
        return await original_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)


def _create_prolific_experiment(client: TestClient) -> tuple[dict, dict]:
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    return experiment, pilot_resp.json()


@respx.mock
def test_prolific_create_stores_study_id(client: TestClient, enable_prolific):
    experiment, pilot = _create_prolific_experiment(client)

    assert pilot["prolific_study_id"] == PROLIFIC_STUDY_ID
    assert pilot["prolific_study_status"] == "UNPUBLISHED"
    assert pilot["prolific_study_url"] is not None
    assert PROLIFIC_STUDY_ID in pilot["prolific_study_url"]

    experiments = client.get("/api/admin/experiments").json()
    stored = next(item for item in experiments if item["id"] == experiment["id"])
    assert stored["prolific_completion_url"] is not None
    rounds = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/rounds").json()
    assert [round_["round_number"] for round_ in rounds] == [0]


@respx.mock
def test_prolific_round_names_include_round_label(client: TestClient, enable_prolific):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    pilot_route = _mock_create_study(study_id="PILOT_STUDY")
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text
    assert pilot_route.called
    pilot_payload = json.loads(pilot_route.calls[-1].request.content.decode())
    assert pilot_payload["name"] == f"{experiment['name']} - Pilot"

    _mock_publish_study(study_id="PILOT_STUDY")
    publish_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/publish"
    )
    assert publish_resp.status_code == 200

    _mock_close_study(study_id="PILOT_STUDY")
    close_resp = client.post(f"/api/admin/experiments/{experiment['id']}/prolific/rounds/1/close")
    assert close_resp.status_code == 200

    round_route = _mock_create_study(study_id="ROUND_1_STUDY")
    round_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 4},
    )
    assert round_resp.status_code == 200, round_resp.text
    assert round_route.called
    round_payload = json.loads(round_route.calls[-1].request.content.decode())
    assert round_payload["name"] == f"{experiment['name']} - Round 1"


@respx.mock
def test_prolific_create_failure_returns_502(client: TestClient, enable_prolific):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    _mock_create_study(status=500)

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )

    assert resp.status_code == 502

    # Experiment remains, but no rounds were created and no study is linked.
    experiments = client.get("/api/admin/experiments").json()
    stored = next(item for item in experiments if item["id"] == experiment["id"])
    assert stored["prolific_completion_url"] is None
    rounds = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/rounds").json()
    assert rounds == []


@respx.mock
def test_prolific_create_includes_project_when_set(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_ABC")
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    assert sent["project"] == "PROJ_ABC"


@respx.mock
def test_prolific_create_omits_project_when_unset(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    # When project_id is empty, the field must be absent from the payload —
    # sending an empty string would 400 from Prolific.
    monkeypatch.setattr(get_settings().prolific, "project_id", "")
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    route = _mock_create_study()
    pilot_resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert pilot_resp.status_code == 200, pilot_resp.text

    sent = json.loads(route.calls[-1].request.content.decode())
    assert "project" not in sent


@respx.mock
def test_prolific_create_failure_propagates_message(client: TestClient, enable_prolific):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    respx.post(f"{PROLIFIC_BASE}/studies/").mock(
        return_value=Response(
            400,
            json={"error": {"detail": "Reward must be at least 100"}},
        )
    )

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )
    assert resp.status_code == 502
    assert "Reward must be at least 100" in resp.json()["detail"]


@respx.mock
def test_prolific_second_pilot_is_rejected(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "A pilot study has already been run for this experiment"


@respx.mock
def test_prolific_pilot_commit_conflict_deletes_orphaned_study(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    create_resp = client.post("/api/admin/experiments", json=_prolific_experiment_payload())
    assert create_resp.status_code == 200, create_resp.text
    experiment = create_resp.json()

    _patch_commit_to_fail_for_round(monkeypatch, round_number=0)
    create_route = _mock_create_study(study_id="PILOT_ORPHAN")
    delete_route = _mock_delete_study(study_id="PILOT_ORPHAN")

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/pilot",
        json=_pilot_payload(),
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "A pilot study has already been run for this experiment"
    assert create_route.called
    assert delete_route.called

    rounds = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/rounds").json()
    assert rounds == []


@respx.mock
def test_prolific_recommendation_returns_zeros_before_ratings(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)

    resp = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/recommend")

    assert resp.status_code == 200
    assert resp.json() == {
        "avg_time_per_question_seconds": 0.0,
        "remaining_rating_actions": 0,
        "total_hours_remaining": 0.0,
        "recommended_places": 0,
        "is_complete": False,
    }


@respx.mock
def test_prolific_recommendation_updates_after_pilot_rating(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    _upload_questions(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_PILOT_RATER")

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    started_at = datetime.now(UTC) - timedelta(seconds=45)
    submit_resp = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": started_at.isoformat(),
        },
    )
    assert submit_resp.status_code == 200

    resp = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/recommend")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["avg_time_per_question_seconds"] > 0
    assert payload["remaining_rating_actions"] == 3
    assert payload["total_hours_remaining"] > 0
    assert payload["recommended_places"] == 1
    assert payload["is_complete"] is False


@respx.mock
def test_prolific_recommendation_honors_include_preview(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    _upload_questions(client, experiment["id"])

    response = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment["id"],
            "PROLIFIC_PID": "PID_PREVIEW",
            "STUDY_ID": "STUDY_PREVIEW",
            "SESSION_ID": "SESSION_PREVIEW",
            "preview": "true",
        },
    )
    assert response.status_code == 200
    session_payload = response.json()

    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    submit_resp = client.post(
        "/api/raters/submit",
        headers=_rater_headers(session_payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        },
    )
    assert submit_resp.status_code == 200

    default_resp = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/recommend")
    preview_resp = client.get(
        f"/api/admin/experiments/{experiment['id']}/prolific/recommend?include_preview=true"
    )

    assert default_resp.status_code == 200
    assert default_resp.json()["avg_time_per_question_seconds"] == 0.0
    assert preview_resp.status_code == 200
    assert preview_resp.json()["avg_time_per_question_seconds"] > 0


@respx.mock
def test_prolific_round_requires_pilot(client: TestClient, enable_prolific):
    experiment = _create_experiment(client)

    resp = client.post(
        f"/api/admin/experiments/{experiment['id']}/prolific/rounds",
        json={"places": 4},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Run a pilot study first before launching a main round"


@respx.mock
def test_prolific_round_creation_requires_closing_previous_round(
    client: TestClient,
    enable_prolific,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    before_publish = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )
    assert before_publish.status_code == 400
    assert (
        before_publish.json()["detail"] == "Close the previous round before launching a new round"
    )

    _mock_publish_study()
    publish_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_resp.status_code == 200

    while_active = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )
    assert while_active.status_code == 400
    assert while_active.json()["detail"] == "Close the previous round before launching a new round"

    _mock_close_study()
    close_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close")
    assert close_resp.status_code == 200
    assert close_resp.json()["status"] == "AWAITING_REVIEW"

    _mock_create_study(study_id="ROUND_STUDY")
    first_round = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )
    assert first_round.status_code == 200, first_round.text
    assert first_round.json()["round_number"] == 1


@respx.mock
def test_prolific_round_commit_conflict_deletes_orphaned_study(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish").status_code
        == 200
    )
    _mock_close_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close").status_code
        == 200
    )

    _patch_commit_to_fail_for_round(monkeypatch, round_number=1)
    create_route = _mock_create_study(study_id="ROUND_ORPHAN")
    delete_route = _mock_delete_study(study_id="ROUND_ORPHAN")

    resp = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "A round with this number already exists for this experiment"
    assert create_route.called
    assert delete_route.called

    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert [round_["round_number"] for round_ in rounds] == [0]


@respx.mock
def test_prolific_round_commit_conflict_preserves_409_when_cleanup_fails(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish").status_code
        == 200
    )
    _mock_close_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close").status_code
        == 200
    )

    _patch_commit_to_fail_for_round(monkeypatch, round_number=1)
    _mock_create_study(study_id="ROUND_ORPHAN_DELETE_FAIL")
    delete_route = _mock_delete_study(study_id="ROUND_ORPHAN_DELETE_FAIL", status=500)

    resp = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "A round with this number already exists for this experiment"
    assert delete_route.called
    assert "Failed to clean up orphaned Prolific study after local DB failure" in caplog.text


@respx.mock
def test_prolific_round_history_and_completion_url_progression(
    client: TestClient,
    enable_prolific,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]
    initial_experiment = next(
        item for item in client.get("/api/admin/experiments").json() if item["id"] == experiment_id
    )
    initial_completion_url = initial_experiment["prolific_completion_url"]

    _mock_publish_study()
    publish_pilot = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_pilot.status_code == 200

    _mock_close_study()
    close_pilot = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close")
    assert close_pilot.status_code == 200

    _mock_create_study(study_id="ROUND_STUDY_1")
    round_one = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 4},
    )
    assert round_one.status_code == 200

    _mock_publish_study(study_id="ROUND_STUDY_1")
    publish_round_one = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/2/publish"
    )
    assert publish_round_one.status_code == 200

    _mock_close_study(study_id="ROUND_STUDY_1", closed_status="COMPLETED")
    close_round_one = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/2/close")
    assert close_round_one.status_code == 200

    _mock_create_study(study_id="ROUND_STUDY_2")
    round_two = client.post(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds",
        json={"places": 2},
    )
    assert round_two.status_code == 200

    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert [round_["round_number"] for round_ in rounds] == [0, 1, 2]
    assert [round_["places_requested"] for round_ in rounds] == [5, 4, 2]

    stored = next(
        item for item in client.get("/api/admin/experiments").json() if item["id"] == experiment_id
    )
    assert stored["prolific_completion_url"] == initial_completion_url


@respx.mock
def test_prolific_round_publish_updates_status(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    route = _mock_publish_study()
    resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")

    assert resp.status_code == 200
    assert route.called
    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert rounds[0]["prolific_study_status"] == "ACTIVE"


@respx.mock
def test_prolific_round_edit_updates_db_and_calls_prolific(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    route = _mock_update_study()
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={"description": "Updated description", "reward": 1500, "places": 7},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "Updated description"
    assert body["reward"] == 1500
    assert body["places_requested"] == 7
    assert route.called

    sent = json.loads(route.calls[0].request.content)
    assert sent == {
        "description": "Updated description",
        "reward": 1500,
        "total_available_places": 7,
    }


@respx.mock
def test_prolific_round_edit_rejects_when_published(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    publish_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_resp.status_code == 200

    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={"description": "Cannot edit"},
    )

    assert resp.status_code == 400
    assert "unpublished" in resp.json()["detail"].lower()


@respx.mock
def test_prolific_round_edit_rejects_empty_payload(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={},
    )

    assert resp.status_code == 400


@respx.mock
def test_prolific_round_edit_returns_404_for_missing_round(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/9999",
        json={"description": "x"},
    )

    assert resp.status_code == 404


@respx.mock
def test_prolific_round_edit_returns_502_when_prolific_fails(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_update_study(status=500)
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}/prolific/rounds/1",
        json={"description": "x"},
    )

    assert resp.status_code == 502


@respx.mock
def test_prolific_round_list_refreshes_transient_status_from_prolific(
    client: TestClient,
    enable_prolific,
):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    respx.post(f"{PROLIFIC_BASE}/studies/{PROLIFIC_STUDY_ID}/transition/").mock(
        return_value=Response(200, json={"id": PROLIFIC_STUDY_ID, "status": "PUBLISHING"})
    )
    publish_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_resp.status_code == 200
    assert publish_resp.json()["status"] == "PUBLISHING"

    route = _mock_get_study(study_status="ACTIVE")
    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()

    assert route.called
    assert rounds[0]["prolific_study_status"] == "ACTIVE"


@respx.mock
def test_prolific_round_close_updates_status(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    publish_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish")
    assert publish_resp.status_code == 200

    route = _mock_close_study(closed_status="AWAITING_REVIEW")
    close_resp = client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close")

    assert close_resp.status_code == 200
    assert close_resp.json()["status"] == "AWAITING_REVIEW"
    assert route.called

    rounds = client.get(f"/api/admin/experiments/{experiment_id}/prolific/rounds").json()
    assert rounds[0]["prolific_study_status"] == "AWAITING_REVIEW"


@respx.mock
def test_prolific_delete_calls_prolific_api_for_all_rounds(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_publish_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/publish").status_code
        == 200
    )
    _mock_close_study()
    assert (
        client.post(f"/api/admin/experiments/{experiment_id}/prolific/rounds/1/close").status_code
        == 200
    )
    _mock_create_study(study_id="ROUND_STUDY_DELETE")
    assert (
        client.post(
            f"/api/admin/experiments/{experiment_id}/prolific/rounds",
            json={"places": 4},
        ).status_code
        == 200
    )

    pilot_delete = _mock_delete_study(study_id=PROLIFIC_STUDY_ID)
    round_delete = _mock_delete_study(study_id="ROUND_STUDY_DELETE")

    resp = client.delete(f"/api/admin/experiments/{experiment_id}")

    assert resp.status_code == 200
    assert pilot_delete.called
    assert round_delete.called

    experiments = client.get("/api/admin/experiments").json()
    assert all(e["id"] != experiment_id for e in experiments)


@respx.mock
def test_prolific_delete_succeeds_when_api_fails(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_delete_study(status=500)

    resp = client.delete(f"/api/admin/experiments/{experiment_id}")

    # Local delete succeeds even when Prolific API fails.
    assert resp.status_code == 200


@respx.mock
def test_prolific_delete_handles_404(client: TestClient, enable_prolific):
    experiment, _pilot = _create_prolific_experiment(client)
    experiment_id = experiment["id"]

    _mock_delete_study(status=404)

    resp = client.delete(f"/api/admin/experiments/{experiment_id}")

    assert resp.status_code == 200


def test_platform_status_reflects_prolific_enabled(client: TestClient, enable_prolific):
    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    assert resp.json()["prolific_enabled"] is True


def test_platform_status_disabled_by_default(client: TestClient):
    settings = get_settings()
    original = settings.prolific.api_token
    settings.prolific.api_token = ""
    try:
        resp = client.get("/api/admin/platform-status")
        assert resp.status_code == 200
        assert resp.json()["prolific_enabled"] is False
    finally:
        settings.prolific.api_token = original


@respx.mock
def test_platform_status_returns_workspace_currency(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_ABC")

    _mock_project(project_id="PROJ_ABC", workspace_id="WS_ABC")
    _mock_workspace_balance(workspace_id="WS_ABC", currency_code="USD")

    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency_code"] == "USD"
    assert body["currency_symbol"] == "$"


def test_platform_status_currency_null_when_project_id_unset(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "")

    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency_code"] is None
    assert body["currency_symbol"] is None


@respx.mock
def test_platform_status_currency_null_when_project_lookup_fails(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_NOT_FOUND")

    _mock_project(project_id="PROJ_NOT_FOUND", status=404)
    balance_route = _mock_workspace_balance(workspace_id="WS_ABC")

    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency_code"] is None
    assert body["currency_symbol"] is None
    # Project lookup fails before we learn the workspace, so balance is never queried.
    assert not balance_route.called


@respx.mock
def test_platform_status_currency_null_when_balance_fails(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_ABC")

    _mock_project(project_id="PROJ_ABC", workspace_id="WS_ABC")
    _mock_workspace_balance(workspace_id="WS_ABC", status=500)

    resp = client.get("/api/admin/platform-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency_code"] is None
    assert body["currency_symbol"] is None


@respx.mock
def test_platform_status_currency_cached_across_calls(
    client: TestClient,
    enable_prolific,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings().prolific, "project_id", "PROJ_ABC")

    project_route = _mock_project(project_id="PROJ_ABC", workspace_id="WS_ABC")
    balance_route = _mock_workspace_balance(workspace_id="WS_ABC", currency_code="GBP")

    resp1 = client.get("/api/admin/platform-status")
    resp2 = client.get("/api/admin/platform-status")

    assert resp1.json()["currency_code"] == "GBP"
    assert resp1.json()["currency_symbol"] == "£"
    assert resp2.json()["currency_code"] == "GBP"
    assert project_route.call_count == 1
    assert balance_route.call_count == 1


# ── parent_question_id (sub-questions) ────────────────────────────────────────

PARENT_TEXT = "Customer review: arrived late but exceeded expectations."


def _upload_parent_and_children(client: TestClient, experiment_id: int) -> None:
    csv_data = (
        "question_id,question_text,gt_answer,options,question_type,parent_question_id\n"
        f'parent1,"{PARENT_TEXT}",,,,\n'
        "sub_satisfied,Does the review express satisfaction?,Yes,Yes|No,MC,parent1\n"
        "sub_problem,Does the review describe a delivery problem?,Yes,Yes|No,MC,parent1\n"
    )
    response = client.post(
        f"/api/admin/experiments/{experiment_id}/upload",
        files={"file": ("questions.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 200, response.text


def test_upload_with_parent_question_id_attaches_context_to_children(client: TestClient):
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])

    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_PARENT_CTX")
    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    # Both eligible questions are children; either should carry the parent text.
    assert question["question_id"] in {"sub_satisfied", "sub_problem"}
    assert question["parent_question_text"] == PARENT_TEXT


def test_upload_without_parent_question_id_returns_null_context(client: TestClient):
    """Backwards compat: standalone questions still have parent_question_text == None."""
    experiment = _create_experiment(client)
    _upload_questions(client, experiment["id"])  # legacy CSV without the column

    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_LEGACY")
    question = client.get(
        "/api/raters/next-question",
        headers=_rater_headers(session_payload),
    ).json()

    assert question["parent_question_text"] is None


def test_upload_rejects_self_referential_parent(client: TestClient):
    experiment = _create_experiment(client)
    csv_data = "question_id,question_text,parent_question_id\nloop,Self-referential question,loop\n"
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("self_ref.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 400
    assert "cannot reference itself" in response.json()["detail"]


def test_upload_rejects_unresolvable_parent_reference(client: TestClient):
    experiment = _create_experiment(client)
    csv_data = (
        "question_id,question_text,parent_question_id\n"
        "orphan,Child without a parent,does_not_exist\n"
    )
    response = client.post(
        f"/api/admin/experiments/{experiment['id']}/upload",
        files={"file": ("orphan.csv", csv_data, "text/csv")},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "does_not_exist" in detail
    assert "orphan" in detail


def test_next_question_never_returns_parent_rows(client: TestClient):
    """Parents are header rows; the rater should only ever see children."""
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_NO_PARENT")
    headers = _rater_headers(session_payload)

    seen_question_ids: set[str] = set()
    for _ in range(2):
        resp = client.get("/api/raters/next-question", headers=headers)
        assert resp.status_code == 200
        question = resp.json()
        seen_question_ids.add(question["question_id"])
        client.post(
            "/api/raters/submit",
            headers=headers,
            json={
                "question_id": question["id"],
                "answer": "Yes",
                "confidence": 4,
                "time_started": datetime.now(UTC).isoformat(),
            },
        )

    assert seen_question_ids == {"sub_satisfied", "sub_problem"}
    assert "parent1" not in seen_question_ids


def test_stats_total_questions_excludes_parent_rows(client: TestClient):
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])

    stats = client.get(f"/api/admin/experiments/{experiment['id']}/stats").json()
    # parent1 + 2 children uploaded, but parent1 is not ratable.
    assert stats["total_questions"] == 2


def test_list_experiments_question_count_excludes_parent_rows(client: TestClient):
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])

    items = client.get("/api/admin/experiments").json()
    matching = next(item for item in items if item["id"] == experiment["id"])
    assert matching["question_count"] == 2


def test_recommendation_remaining_actions_excludes_parent_rows(client: TestClient):
    """Bug guard: ghost parent rows used to inflate recommended_places."""
    experiment = _create_experiment(client)
    _upload_parent_and_children(client, experiment["id"])

    # num_ratings_per_question defaults to 2 in _create_experiment, 2 children → 4 actions total.
    session_payload = _start_session(client, experiment["id"], prolific_pid="PID_REC")
    headers = _rater_headers(session_payload)
    question = client.get("/api/raters/next-question", headers=headers).json()
    submit = client.post(
        "/api/raters/submit",
        headers=headers,
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        },
    )
    assert submit.status_code == 200

    resp = client.get(f"/api/admin/experiments/{experiment['id']}/prolific/recommend")
    assert resp.status_code == 200
    payload = resp.json()
    # 2 children × 2 ratings - 1 submitted = 3. (Pre-fix this was 5: 3 questions × 2 - 1.)
    assert payload["remaining_rating_actions"] == 3
