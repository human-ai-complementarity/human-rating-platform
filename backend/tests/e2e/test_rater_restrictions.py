"""`block_copy_paste` and `forbid_outside_tools`: per-experiment rater restrictions.

Set at create or by PATCH, copied by a duplicate, and sent to the rater with
the session so the rating and instruction screens can apply them. The lock
after launch is covered with the rendering flags in test_characterization.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

FIELDS = ["block_copy_paste", "forbid_outside_tools"]


def _create(client: TestClient, **fields) -> dict:
    resp = client.post(
        "/api/admin/experiments",
        json={"name": "Rater restrictions", "assistance_method": "none", **fields},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _patch(client: TestClient, experiment_id: int, **fields) -> dict:
    resp = client.patch(
        f"/api/admin/experiments/{experiment_id}",
        json={"assistance_method": "none", **fields},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _start(client: TestClient, experiment_id: int, pid: str) -> dict:
    resp = client.post(
        "/api/raters/start",
        params={
            "experiment_id": experiment_id,
            "PROLIFIC_PID": pid,
            "STUDY_ID": "STUDY_1",
            "SESSION_ID": f"SESSION_{pid}",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("field", FIELDS)
def test_restriction_is_off_by_default_and_set_at_create(client: TestClient, field):
    assert _create(client)[field] is False
    assert _create(client, **{field: True})[field] is True


@pytest.mark.parametrize("field", FIELDS)
def test_restriction_patches_on_and_off_and_survives_other_edits(client: TestClient, field):
    experiment = _create(client)
    assert _patch(client, experiment["id"], **{field: True})[field] is True
    assert _patch(client, experiment["id"], description="unrelated edit")[field] is True
    assert _patch(client, experiment["id"], **{field: False})[field] is False


@pytest.mark.parametrize("field", FIELDS)
def test_restriction_is_copied_by_duplicate(client: TestClient, field):
    experiment = _create(client, **{field: True})
    resp = client.post(f"/api/admin/experiments/{experiment['id']}/duplicate")
    assert resp.status_code == 200, resp.text
    assert resp.json()[field] is True


@pytest.mark.parametrize("field", FIELDS)
def test_restriction_reaches_new_and_returning_raters(client: TestClient, field):
    experiment = _create(client)
    assert _start(client, experiment["id"], f"PID_EARLY_{field}")[field] is False

    _patch(client, experiment["id"], **{field: True})

    assert _start(client, experiment["id"], f"PID_LATE_{field}")[field] is True
    assert _start(client, experiment["id"], f"PID_EARLY_{field}")[field] is True
