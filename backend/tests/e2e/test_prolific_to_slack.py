"""Prolific -> Slack message forwarding against a real Postgres.

Every outbound HTTP call is mocked with respx (unmocked requests raise), so
these never reach Prolific or Slack.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import Settings, get_settings
from models import Experiment, ExperimentRound, ForwardedProlificMessage, ProlificStudyStatus
from services.prolific_to_slack import forwarding_enabled, run_forwarding_tick

PROLIFIC_BASE = "https://api.prolific.com/api/v1"
WEBHOOK_URL = "https://hooks.slack.test/services/T000/B000/XXXX"
OWN_USER_ID = "researcher-1"
STUDY_ID = "study-abc"


@pytest.fixture
def settings() -> Settings:
    settings = get_settings().model_copy(deep=True)
    settings.prolific.api_token = "test-token"
    settings.prolific.base_url = PROLIFIC_BASE
    settings.prolific.project_id = "project-1"
    settings.slack.webhook_url = WEBHOOK_URL
    return settings


@asynccontextmanager
async def _session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(get_settings().async_database_url)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


def _message(message_id: str, **overrides) -> dict:
    message = {
        "id": message_id,
        "sender_id": "participant-1",
        "body": f"hello from {message_id}",
        "sent_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        "channel_id": f"channel-{message_id}",
        "type": "message",
        "data": {"study_id": STUDY_ID, "category": "payment-issues"},
    }
    message.update(overrides)
    return message


def _mock_prolific(router: respx.MockRouter, messages: list[dict]) -> respx.Route:
    router.get(f"{PROLIFIC_BASE}/projects/project-1/").mock(
        return_value=Response(200, json={"id": "project-1", "workspace": "workspace-1"})
    )
    router.get(f"{PROLIFIC_BASE}/users/me/").mock(
        return_value=Response(200, json={"id": OWN_USER_ID})
    )
    return router.get(f"{PROLIFIC_BASE}/messages/").mock(
        return_value=Response(200, json={"results": messages})
    )


async def _forwarded_ids(Session: async_sessionmaker[AsyncSession]) -> set[str]:
    async with Session() as session:
        return set(
            (await session.execute(select(ForwardedProlificMessage.prolific_message_id))).scalars()
        )


def _slack_texts(route: respx.Route) -> list[str]:
    return [json.loads(call.request.content)["text"] for call in route.calls]


@pytest.mark.asyncio
async def test_new_messages_forwarded_once(settings: Settings) -> None:
    async with _session_factory() as Session:
        async with Session() as session:
            experiment = Experiment(name="Fact check pilot", num_ratings_per_question=1)
            session.add(experiment)
            await session.flush()
            session.add(
                ExperimentRound(
                    experiment_id=experiment.id,
                    round_number=1,
                    prolific_study_id=STUDY_ID,
                    prolific_study_status=ProlificStudyStatus.ACTIVE,
                    description="d",
                    estimated_completion_time=10,
                    reward=100,
                    device_compatibility='["desktop"]',
                    places_requested=10,
                )
            )
            await session.commit()
            experiment_id = experiment.id

        bare = _message("bare", data=None, channel_id=None)
        with respx.mock(assert_all_called=False) as router:
            messages_route = _mock_prolific(router, [_message("m1"), bare])
            slack = router.post(WEBHOOK_URL).mock(return_value=Response(200))

            await run_forwarding_tick(Session, settings)
            await run_forwarding_tick(Session, settings)

        assert slack.call_count == 2
        bare_text, m1_text = sorted(_slack_texts(slack), key=lambda t: "hello from m1" in t)
        assert (
            f"<{settings.app.site_url}/admin/experiments/{experiment_id}|Fact check pilot> (round 1)"
            in m1_text
        )
        assert "payment-issues" in m1_text
        assert "hello from m1" in m1_text
        assert "no study" in bare_text

        params = messages_route.calls[0].request.url.params
        assert params["workspace_id"] == "workspace-1"
        assert params["created_after"].endswith("Z")
        assert await _forwarded_ids(Session) == {"m1", "bare"}


@pytest.mark.asyncio
async def test_own_messages_are_skipped(settings: Settings) -> None:
    async with _session_factory() as Session:
        with respx.mock(assert_all_called=False) as router:
            _mock_prolific(router, [_message("mine", sender_id=OWN_USER_ID)])
            slack = router.post(WEBHOOK_URL).mock(return_value=Response(200))

            await run_forwarding_tick(Session, settings)

        assert slack.call_count == 0
        assert await _forwarded_ids(Session) == set()


@pytest.mark.asyncio
async def test_slack_failure_releases_claim_and_retries_next_poll(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    async with _session_factory() as Session:
        with respx.mock(assert_all_called=False) as router:
            _mock_prolific(router, [_message("m1")])
            slack = router.post(WEBHOOK_URL).mock(return_value=Response(500))

            with caplog.at_level("ERROR"):
                await run_forwarding_tick(Session, settings)
            assert "poll failed" in caplog.text
            # The webhook URL is a credential; it must not reach the logs.
            assert WEBHOOK_URL not in caplog.text
            assert await _forwarded_ids(Session) == set()

            slack.return_value = Response(200)
            await run_forwarding_tick(Session, settings)

        assert slack.call_count == 2
        assert await _forwarded_ids(Session) == {"m1"}


@pytest.mark.asyncio
async def test_cleanup_deletes_rows_past_retention(settings: Settings, sync_engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO forwarded_prolific_messages (prolific_message_id, forwarded_at) VALUES "
                "('old', now() - interval '3 days'), ('recent', now() - interval '1 day')"
            )
        )
    async with _session_factory() as Session:
        with respx.mock(assert_all_called=False) as router:
            _mock_prolific(router, [])
            await run_forwarding_tick(Session, settings)

        assert await _forwarded_ids(Session) == {"recent"}


@pytest.mark.asyncio
async def test_message_claimed_by_another_process_is_skipped(
    settings: Settings, sync_engine
) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO forwarded_prolific_messages (prolific_message_id) VALUES ('m1')")
        )
    async with _session_factory() as Session:
        with respx.mock(assert_all_called=False) as router:
            _mock_prolific(router, [_message("m1"), _message("m2")])
            slack = router.post(WEBHOOK_URL).mock(return_value=Response(200))

            await run_forwarding_tick(Session, settings)

        assert slack.call_count == 1
        assert "hello from m2" in _slack_texts(slack)[0]
        assert await _forwarded_ids(Session) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_prolific_error_is_logged(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    async with _session_factory() as Session:
        with respx.mock(assert_all_called=False) as router:
            _mock_prolific(router, []).return_value = Response(503)

            with caplog.at_level("ERROR"):
                await run_forwarding_tick(Session, settings)

        assert "poll failed" in caplog.text


def test_forwarding_requires_token_and_webhook(settings: Settings) -> None:
    assert forwarding_enabled(settings)

    no_webhook = settings.model_copy(deep=True)
    no_webhook.slack.webhook_url = ""
    assert not forwarding_enabled(no_webhook)

    no_token = settings.model_copy(deep=True)
    no_token.prolific.api_token = ""
    assert not forwarding_enabled(no_token)
