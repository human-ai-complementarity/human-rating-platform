from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from config import Settings
from routers.deps import require_rater_session
from services.rater.session_token import issue_rater_session_token


def test_require_rater_session_rejects_mismatched_experiment_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token whose experiment_id does not match the rater's persisted experiment_id is rejected.

    This proves the binding check inside require_rater_session is enforced.
    """

    # Arrange: token signed for rid=1, eid=999
    settings = Settings(app_secret_key="secret-key")
    token = issue_rater_session_token(settings, rater_id=1, experiment_id=999)

    # Mock DB lookup to return a rater bound to a different experiment (eid=123)
    async def _fake_fetch_rater_or_404(rater_id: int, db: object):  # pragma: no cover - simple stub
        assert rater_id == 1
        return SimpleNamespace(experiment_id=123)

    monkeypatch.setattr("routers.deps.fetch_rater_or_404", _fake_fetch_rater_or_404)

    # Act + Assert: dependency rejects with 401
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_rater_session(x_rater_session=token, settings=settings, db=object()))

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid rater session"
