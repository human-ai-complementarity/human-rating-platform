from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from config import get_settings
from models import ExperimentRound, ProlificStudyStatus
from test_consent import _consent, _create_experiment, _headers, _start, _upload

PROLIFIC_BASE = "https://api.prolific.com/api/v1"
STUDY_ID = "study-country-1"

_DEMOGRAPHICS = (
    "Submission id,Participant id,Status,Fluent languages,Fact-checking,"
    "Country of birth,Country of residence,Nationality\n"
    's1,PID_COUNTRY,APPROVED,"English, Dutch",Yes,United States,Netherlands,United Kingdom\n'
    "s2,PID_OTHER,RETURNED,English,No,France,France,France\n"
)


@pytest.fixture
def disable_prolific():
    settings = get_settings()
    original_token = settings.prolific.api_token
    settings.prolific.api_token = ""
    yield
    settings.prolific.api_token = original_token


def _rated_experiment(client: TestClient, sync_engine, pid: str) -> int:
    experiment_id = _create_experiment(client)
    _upload(client, experiment_id)
    payload = _start(client, experiment_id, pid)
    _consent(client, payload)
    question = client.get("/api/raters/next-question", headers=_headers(payload)).json()
    submitted = client.post(
        "/api/raters/submit",
        headers=_headers(payload),
        json={
            "question_id": question["id"],
            "answer": "Yes",
            "confidence": 4,
            "time_started": (datetime.now(UTC) - timedelta(seconds=20)).isoformat(),
        },
    )
    assert submitted.status_code == 200, submitted.text
    with Session(sync_engine) as session:
        session.add(
            ExperimentRound(
                experiment_id=experiment_id,
                round_number=1,
                prolific_study_id=STUDY_ID,
                prolific_study_status=ProlificStudyStatus.COMPLETED,
                description="d",
                estimated_completion_time=10,
                reward=100,
                device_compatibility='["desktop"]',
                places_requested=10,
            )
        )
        session.commit()
    return experiment_id


def _export_rows(client: TestClient, experiment_id: int) -> list[dict[str, str]]:
    export = client.get(f"/api/admin/experiments/{experiment_id}/export")
    assert export.status_code == 200, export.text
    return list(csv.DictReader(io.StringIO(export.text)))


def test_export_carries_country_of_residence_from_prolific(
    client: TestClient, terms_source, sync_engine
):
    experiment_id = _rated_experiment(client, sync_engine, "PID_COUNTRY")

    with respx.mock:
        route = respx.post(f"{PROLIFIC_BASE}/studies/{STUDY_ID}/demographic-export/").mock(
            return_value=Response(200, text=_DEMOGRAPHICS)
        )
        rows = _export_rows(client, experiment_id)

    assert route.called
    assert [row["rater_country_of_residence"] for row in rows] == ["Netherlands"]


def test_export_leaves_country_blank_when_prolific_fails(
    client: TestClient, terms_source, sync_engine
):
    experiment_id = _rated_experiment(client, sync_engine, "PID_COUNTRY")

    with respx.mock:
        respx.post(f"{PROLIFIC_BASE}/studies/{STUDY_ID}/demographic-export/").mock(
            return_value=Response(500, text="boom")
        )
        rows = _export_rows(client, experiment_id)

    assert [row["rater_country_of_residence"] for row in rows] == [""]


def test_export_leaves_country_blank_without_prolific(
    client: TestClient, terms_source, sync_engine, disable_prolific
):
    experiment_id = _rated_experiment(client, sync_engine, "PID_COUNTRY")

    rows = _export_rows(client, experiment_id)

    assert [row["rater_country_of_residence"] for row in rows] == [""]
