from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from config import get_settings
from main import create_app

API_KEY = "test-v1-key"


@pytest.fixture
def v1_client(sync_engine):
    """App wired with a configured v1 API key. `sync_engine` (session-scoped)
    is depended on so the test DB is created and `reset_database` runs first."""
    settings = get_settings()
    original_keys = settings.api_keys
    original_admin_auth = settings.admin_auth_enabled
    original_token = settings.prolific.api_token
    settings.api_keys = [API_KEY]
    settings.admin_auth_enabled = False
    if not settings.prolific.api_token:
        settings.prolific.api_token = "test-token"
    app = create_app()
    with TestClient(app) as client:
        yield client
    settings.api_keys = original_keys
    settings.admin_auth_enabled = original_admin_auth
    settings.prolific.api_token = original_token


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


def _insert_experiment(
    sync_engine,
    *,
    name: str,
    num_ratings_per_question: int = 3,
    archived: bool = False,
) -> int:
    with sync_engine.begin() as conn:
        return conn.execute(
            text(
                """
                INSERT INTO experiments (name, num_ratings_per_question, archived_at)
                VALUES (
                    :name,
                    :nrpq,
                    CASE WHEN :archived THEN NOW() ELSE NULL END
                )
                RETURNING id
                """
            ),
            {"name": name, "nrpq": num_ratings_per_question, "archived": archived},
        ).scalar_one()


def _insert_question(sync_engine, *, experiment_id: int, question_id: str, gt: str) -> int:
    with sync_engine.begin() as conn:
        return conn.execute(
            text(
                """
                INSERT INTO questions (
                    experiment_id, question_id, question_text, gt_answer, question_type
                )
                VALUES (:eid, :qid, :qtext, :gt, 'MC')
                RETURNING id
                """
            ),
            {
                "eid": experiment_id,
                "qid": question_id,
                "qtext": f"Full text for {question_id}",
                "gt": gt,
            },
        ).scalar_one()


def _insert_rating(
    sync_engine,
    *,
    experiment_id: int,
    question_db_id: int,
    prolific_id: str,
    answer: str,
    is_preview: bool = False,
    submit_offset_seconds: int = 0,
) -> None:
    with sync_engine.begin() as conn:
        rater_id = conn.execute(
            text(
                """
                INSERT INTO raters (
                    prolific_id, study_id, session_id, experiment_id, session_start, is_preview
                )
                VALUES (:pid, 'STUDY', :sid, :eid, NOW(), :preview)
                RETURNING id
                """
            ),
            {
                "pid": prolific_id,
                "sid": f"SESSION_{prolific_id}",
                "eid": experiment_id,
                "preview": is_preview,
            },
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO ratings (
                    question_id, rater_id, answer, confidence, time_started, time_submitted
                )
                VALUES (
                    :qid,
                    :rid,
                    :answer,
                    3,
                    NOW(),
                    NOW() + (:offset || ' seconds')::interval
                )
                """
            ),
            {
                "qid": question_db_id,
                "rid": rater_id,
                "answer": answer,
                "offset": submit_offset_seconds,
            },
        )


# --- auth -----------------------------------------------------------------


def test_missing_bearer_is_rejected(v1_client: TestClient) -> None:
    resp = v1_client.get("/api/v1/experiments")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing Bearer token"


def test_invalid_api_key_is_rejected(v1_client: TestClient) -> None:
    resp = v1_client.get("/api/v1/experiments", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid API key"


def test_valid_api_key_unlocks_v1(v1_client: TestClient) -> None:
    resp = v1_client.get("/api/v1/experiments", headers=_auth())
    assert resp.status_code == 200


def test_v1_rejects_unknown_key_when_none_configured(sync_engine) -> None:
    # With no env keys and no DB keys, any presented token is simply invalid —
    # the API fails closed.
    settings = get_settings()
    original_keys = settings.api_keys
    original_admin_auth = settings.admin_auth_enabled
    settings.api_keys = []
    settings.admin_auth_enabled = False
    try:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/api/v1/experiments", headers={"Authorization": "Bearer anything"})
            assert resp.status_code == 401
            assert resp.json()["detail"] == "Invalid API key"
    finally:
        settings.api_keys = original_keys
        settings.admin_auth_enabled = original_admin_auth


# --- openapi scope --------------------------------------------------------


def test_openapi_exposes_only_public_v1_by_default(v1_client: TestClient) -> None:
    schema = v1_client.get("/openapi.json").json()
    paths = schema.get("paths", {})
    assert paths, "expected some documented paths"
    assert all(path.startswith("/api/v1") for path in paths), sorted(paths)
    assert "/api/admin/experiments" not in paths
    # Internal request/response models must not leak into components either.
    component_schemas = schema.get("components", {}).get("schemas", {})
    assert "PilotStudyCreate" not in component_schemas
    # The public experiments endpoints use the slim projection, not the admin
    # schema, so internal field names stay out of the shared doc.
    assert "ExperimentResponse" not in component_schemas
    assert "V1ExperimentResponse" in component_schemas


def test_openapi_can_include_internal_docs_via_flag(sync_engine) -> None:
    settings = get_settings()
    original = settings.app.expose_internal_docs
    settings.app.expose_internal_docs = True
    try:
        app = create_app()
        with TestClient(app) as client:
            paths = client.get("/openapi.json").json()["paths"]
            assert any(path.startswith("/api/admin") for path in paths)
            assert any(path.startswith("/api/v1") for path in paths)
    finally:
        settings.app.expose_internal_docs = original


# --- experiments discovery + batch ---------------------------------------


def test_lists_and_batches_experiments(v1_client: TestClient, sync_engine) -> None:
    a = _insert_experiment(sync_engine, name="exp-a")
    b = _insert_experiment(sync_engine, name="exp-b")
    archived = _insert_experiment(sync_engine, name="exp-archived", archived=True)

    listed = v1_client.get("/api/v1/experiments", headers=_auth())
    assert listed.status_code == 200
    ids = {e["id"] for e in listed.json()}
    # Archived rows are hidden from the default list, like the dashboard.
    assert {a, b} <= ids
    assert archived not in ids

    # Batch by ids returns exactly those, including the archived one.
    batch = v1_client.get("/api/v1/experiments", headers=_auth(), params={"ids": [a, archived]})
    assert batch.status_code == 200
    assert {e["id"] for e in batch.json()} == {a, archived}


def test_batch_ids_ignore_pagination_limit(v1_client: TestClient, sync_engine) -> None:
    # An id batch must return every requested experiment even when the batch is
    # larger than `limit` — it's bounded by the id list, not paginated.
    ids = [_insert_experiment(sync_engine, name=f"batch-{i}") for i in range(3)]
    resp = v1_client.get("/api/v1/experiments", headers=_auth(), params={"ids": ids, "limit": 1})
    assert resp.status_code == 200
    assert {e["id"] for e in resp.json()} == set(ids)


def test_get_single_experiment(v1_client: TestClient, sync_engine) -> None:
    eid = _insert_experiment(sync_engine, name="exp-detail")
    resp = v1_client.get(f"/api/v1/experiments/{eid}", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["id"] == eid
    assert resp.json()["name"] == "exp-detail"


# Internal-only fields the admin schema carries that must never reach a v1 client.
_INTERNAL_FIELDS = {
    "internal_name",
    "spend_minor_units",
    "needs_attention",
    "attention_reason",
    "dataset_filenames",
    "prolific_pool",
    "prolific_completion_url",
    "system_prompt",
    "human_prompt_prefix",
    "human_prompt_suffix",
}


def test_experiments_expose_only_public_fields(v1_client: TestClient, sync_engine) -> None:
    with sync_engine.begin() as conn:
        eid = conn.execute(
            text(
                "INSERT INTO experiments (name, internal_name, num_ratings_per_question) "
                "VALUES ('public-name', 'SECRET-internal', 3) RETURNING id"
            )
        ).scalar_one()

    detail = v1_client.get(f"/api/v1/experiments/{eid}", headers=_auth()).json()
    assert detail["name"] == "public-name"
    assert _INTERNAL_FIELDS.isdisjoint(detail), detail.keys()

    listed = v1_client.get("/api/v1/experiments", headers=_auth()).json()
    row = next(e for e in listed if e["id"] == eid)
    assert _INTERNAL_FIELDS.isdisjoint(row), row.keys()


# --- ratings --------------------------------------------------------------


def test_ratings_counts_toward_target_and_full_text(v1_client: TestClient, sync_engine) -> None:
    eid = _insert_experiment(sync_engine, name="exp-ratings", num_ratings_per_question=1)
    qid = _insert_question(sync_engine, experiment_id=eid, question_id="q1", gt="Yes")
    # Two real ratings on the same question; only the first-submitted counts.
    _insert_rating(
        sync_engine,
        experiment_id=eid,
        question_db_id=qid,
        prolific_id="PID_FIRST",
        answer="Yes",
        submit_offset_seconds=0,
    )
    _insert_rating(
        sync_engine,
        experiment_id=eid,
        question_db_id=qid,
        prolific_id="PID_SECOND",
        answer="No",
        submit_offset_seconds=60,
    )

    resp = v1_client.get(f"/api/v1/experiments/{eid}/ratings", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["experiment_id"] == eid
    rows = {r["rater_prolific_id"]: r for r in body["ratings"]}
    assert rows["PID_FIRST"]["counts_toward_target"] is True
    assert rows["PID_SECOND"]["counts_toward_target"] is False
    # Full untruncated question text + ground truth are exposed.
    assert rows["PID_FIRST"]["question_text"] == "Full text for q1"
    assert rows["PID_FIRST"]["gt_answer"] == "Yes"
    assert rows["PID_FIRST"]["question_id"] == "q1"


def test_ratings_pagination(v1_client: TestClient, sync_engine) -> None:
    eid = _insert_experiment(sync_engine, name="exp-page", num_ratings_per_question=3)
    qid = _insert_question(sync_engine, experiment_id=eid, question_id="q1", gt="A")
    for i in range(3):
        _insert_rating(
            sync_engine,
            experiment_id=eid,
            question_db_id=qid,
            prolific_id=f"PID_{i}",
            answer="A",
            submit_offset_seconds=i,
        )

    page1 = v1_client.get(
        f"/api/v1/experiments/{eid}/ratings", headers=_auth(), params={"limit": 2, "offset": 0}
    ).json()
    page2 = v1_client.get(
        f"/api/v1/experiments/{eid}/ratings", headers=_auth(), params={"limit": 2, "offset": 2}
    ).json()
    assert page1["total"] == 3
    assert len(page1["ratings"]) == 2
    assert len(page2["ratings"]) == 1
    seen = {r["rating_id"] for r in page1["ratings"]} | {r["rating_id"] for r in page2["ratings"]}
    assert len(seen) == 3


def test_ratings_exclude_preview_by_default(v1_client: TestClient, sync_engine) -> None:
    eid = _insert_experiment(sync_engine, name="exp-preview", num_ratings_per_question=3)
    qid = _insert_question(sync_engine, experiment_id=eid, question_id="q1", gt="A")
    _insert_rating(
        sync_engine, experiment_id=eid, question_db_id=qid, prolific_id="PID_REAL", answer="A"
    )
    _insert_rating(
        sync_engine,
        experiment_id=eid,
        question_db_id=qid,
        prolific_id="PID_PREVIEW",
        answer="A",
        is_preview=True,
    )

    default = v1_client.get(f"/api/v1/experiments/{eid}/ratings", headers=_auth()).json()
    assert default["total"] == 1
    assert [r["rater_prolific_id"] for r in default["ratings"]] == ["PID_REAL"]

    with_preview = v1_client.get(
        f"/api/v1/experiments/{eid}/ratings",
        headers=_auth(),
        params={"include_preview": "true"},
    ).json()
    assert with_preview["total"] == 2
