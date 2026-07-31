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
from models import SESSION_DURATION_MINUTES

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
                "experiments, api_keys RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def backdate_rater_session(sync_engine):
    def _apply(rater_id: int) -> None:
        with sync_engine.begin() as conn:
            conn.execute(
                text("UPDATE raters SET session_start = :session_start WHERE id = :rater_id"),
                {
                    "session_start": datetime.now(UTC)
                    - timedelta(minutes=SESSION_DURATION_MINUTES + 1),
                    "rater_id": rater_id,
                },
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
