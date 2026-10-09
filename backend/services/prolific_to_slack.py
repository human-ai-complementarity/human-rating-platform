"""Forward Prolific participant messages to Slack with a polling loop."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from models import Experiment, ExperimentRound, ForwardedProlificMessage
from services.admin.prolific import get_current_user, get_user_messages, get_project
from services.slack import escape_mrkdwn, post_webhook_message

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

POLL_INTERVAL_SECONDS = 300
POLL_JITTER_SECONDS = 60
MESSAGE_LOOKBACK_HOURS = 24
# Must exceed MESSAGE_LOOKBACK_HOURS, or a message could be re-posted after its row is deleted.
DEDUP_ROW_RETENTION_HOURS = 48
SLACK_BODY_MAX_CHARS = 1500


def forwarding_enabled(settings: Settings) -> bool:
    return settings.prolific.enabled and settings.slack.enabled


async def _experiment_label_for_study(
    session: AsyncSession, settings: Settings, study_id: str | None
) -> str:
    if not study_id:
        return "no study"
    row = (
        await session.execute(
            select(Experiment.id, Experiment.name, ExperimentRound.round_number)
            .join(ExperimentRound, ExperimentRound.experiment_id == Experiment.id)
            .where(ExperimentRound.prolific_study_id == study_id)
            .limit(1)
        )
    ).first()
    if row is None:
        return f"Prolific study {study_id}"
    round_label = "pilot" if row.round_number == 0 else f"round {row.round_number}"
    url = f"{settings.app.site_url}/admin/experiments/{row.id}"
    return f"<{url}|{escape_mrkdwn(row.name)}> ({round_label})"


async def _format_slack_message(session: AsyncSession, settings: Settings, message: dict) -> str:
    data = message.get("data") or {}
    body = (message.get("body") or "")[:SLACK_BODY_MAX_CHARS]
    experiment = await _experiment_label_for_study(session, settings, data.get("study_id"))
    # TODO: link the title to the thread in Prolific's inbox. Ask Sander for the
    # URL of an open thread, to see whether the message's `channel_id` is in it.
    category = data.get("category") or "no category"
    return f"*Prolific message* · {experiment} · {category}\n>>> {escape_mrkdwn(body)}"


async def _forward_new_messages(session: AsyncSession, settings: Settings) -> None:
    prolific = settings.prolific
    own_id = (await get_current_user(settings=prolific))["id"]
    workspace_id = None
    if prolific.project_id:
        project = await get_project(settings=prolific, project_id=prolific.project_id)
        workspace_id = project["workspace"]
    messages = await get_user_messages(
        settings=prolific,
        created_after=datetime.now(UTC) - timedelta(hours=MESSAGE_LOOKBACK_HOURS),
        workspace_id=workspace_id,
    )
    # Our own replies come back in the same thread; only participants' go to Slack.
    messages = sorted((m for m in messages if m["sender_id"] != own_id), key=lambda m: m["sent_at"])

    for message in messages:
        if not await _claim(session, message["id"]):
            continue
        try:
            await post_webhook_message(
                settings=settings.slack,
                text=await _format_slack_message(session, settings, message),
            )
        except Exception:
            await session.execute(
                delete(ForwardedProlificMessage).where(
                    ForwardedProlificMessage.prolific_message_id == message["id"]
                )
            )
            await session.commit()
            raise
        logger.info(
            "Prolific to Slack: message forwarded",
            extra={"attributes": {"prolific_message_id": message["id"]}},
        )


async def _claim(session: AsyncSession, message_id: str) -> bool:
    """False if another process already claimed it."""
    result = await session.execute(
        pg_insert(ForwardedProlificMessage)
        .values(prolific_message_id=message_id)
        .on_conflict_do_nothing(index_elements=["prolific_message_id"])
    )
    await session.commit()
    return result.rowcount == 1


async def run_forwarding_tick(session_factory: SessionFactory, settings: Settings) -> None:
    """Poll Prolific for messages since MESSAGE_LOOKBACK_HOURS."""
    try:
        async with session_factory() as session:
            await _forward_new_messages(session, settings)
            cutoff = datetime.now(UTC) - timedelta(hours=DEDUP_ROW_RETENTION_HOURS)
            await session.execute(
                delete(ForwardedProlificMessage).where(
                    ForwardedProlificMessage.forwarded_at < cutoff
                )
            )
            await session.commit()
    except Exception:
        logger.error("Prolific to Slack: poll failed", exc_info=True)


async def run_forwarding_loop(session_factory: SessionFactory, settings: Settings) -> None:
    """Poll forever; cancel the task to stop."""
    await asyncio.sleep(random.uniform(0, POLL_JITTER_SECONDS))
    while True:
        await run_forwarding_tick(session_factory, settings)
        await asyncio.sleep(POLL_INTERVAL_SECONDS + random.uniform(0, POLL_JITTER_SECONDS))
