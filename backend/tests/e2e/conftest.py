from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

from config import get_settings
from main import create_app
from session_policy import (
    DEFAULT_GRACE_MINUTES,
    DEFAULT_SESSION_DURATION_MINUTES,
)

_TEST_DB_NAME = "human_rating_platform_test"


def _replace_db_name(url: str, new_name: str) -> str:
    return make_url(url).set(database=new_name).render_as_string(hide_password=False)


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create and migrate an isolated test database, then point all settings at it."""
    from alembic import command as alembic_command
    from alembic.config import Config

    dev_url = get_settings().sync_database_url
    test_url = _replace_db_name(dev_url, _TEST_DB_NAME)
    admin_url = _replace_db_name(dev_url, "postgres")

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {_TEST_DB_NAME}"))

    backend_dir = Path(__file__).resolve().parents[2]
    alembic_cfg = Config(str(backend_dir / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", test_url)
    alembic_command.upgrade(alembic_cfg, "head")

    os.environ["DATABASE__URL"] = test_url
    get_settings.cache_clear()

    yield

    os.environ.pop("DATABASE__URL", None)


@pytest.fixture(scope="session")
def sync_engine(test_database):
    settings = get_settings()
    return create_engine(settings.sync_database_url, pool_pre_ping=True)


@pytest.fixture(autouse=True)
def reset_database(sync_engine):
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE experiment_rounds, ratings, raters, questions, uploads, "
                "experiments, api_keys, experiment_groups, datasets RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def backdate_rater_session(sync_engine):
    def _apply(
        rater_id: int,
        minutes_ago: int | None = None,
        *,
        duration_minutes: int = DEFAULT_SESSION_DURATION_MINUTES,
        grace_minutes: int = DEFAULT_GRACE_MINUTES,
    ) -> None:
        """Move a rater's session_start into the past.

        Defaults to clearing the hard deadline, i.e. the session is over in
        every sense. Pass `minutes_ago` to land somewhere specific — between
        the deadline and the hard deadline is the grace window, where new
        questions are refused but the one in hand can still be submitted.

        The durations are parameters rather than module constants so a test
        using a non-default session length still backdates past its own
        deadline instead of the default one.
        """
        if minutes_ago is None:
            minutes_ago = duration_minutes + grace_minutes + 1
        with sync_engine.begin() as conn:
            conn.execute(
                text("UPDATE raters SET session_start = :session_start WHERE id = :rater_id"),
                {
                    "session_start": datetime.now(UTC) - timedelta(minutes=minutes_ago),
                    "rater_id": rater_id,
                },
            )
            # Age this rater's reservations by the same amount. Moving only
            # session_start models a rater who has been going for an hour but
            # was handed their question seconds ago, which no real rater is —
            # and it hides bugs where the reservation TTL lapses before the
            # session deadline does.
            conn.execute(
                text(
                    """
                    UPDATE question_assignments
                       SET assigned_at = assigned_at - make_interval(mins => :mins),
                           expires_at  = expires_at  - make_interval(mins => :mins)
                     WHERE rater_id = :rater_id
                    """
                ),
                {"mins": minutes_ago, "rater_id": rater_id},
            )

    return _apply


@pytest.fixture
def client():
    settings = get_settings()
    original_token = settings.prolific.api_token
    original_admin_auth = settings.admin_auth_enabled
    if not settings.prolific.api_token:
        settings.prolific.api_token = "test-token"
    settings.admin_auth_enabled = False
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    settings.prolific.api_token = original_token
    settings.admin_auth_enabled = original_admin_auth
