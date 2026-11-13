"""The worker process's loop: claim queued pilot check runs and run them.

With `pilot_check.runner = "worker"` the API only queues runs. This loop,
in its own process (`python -m pilot_check_worker`, a separate Render
service), claims them from Postgres and runs at most `worker_concurrency` at
once; the rest wait in the queue. A run's LLM calls, provider slots and
memory are this process's, never the API's that serves raters.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from config import PilotCheckSettings
from database import Database

from . import runs

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0


async def fill(database: Database, running: set[asyncio.Task], concurrency: int) -> None:
    """Claim runs until `concurrency` are going or the queue is empty."""
    while len(running) < concurrency:
        run_id = await runs.claim_next(database)
        if run_id is None:
            return
        logger.info("pilot check run claimed", extra={"attributes": {"run_id": run_id}})
        task = asyncio.create_task(runs.execute_run(run_id, database), name=f"pilot-check-{run_id}")
        running.add(task)
        task.add_done_callback(running.discard)


async def serve(
    database: Database,
    settings: PilotCheckSettings,
    stop: asyncio.Event,
    *,
    poll_seconds: float = POLL_SECONDS,
) -> None:
    """Claim and run until `stop` is set, then cancel what is still going;
    a cancelled run marks its row aborted (`runs.execute_run`)."""
    running: set[asyncio.Task] = set()
    logger.info(
        "pilot check worker started",
        extra={"attributes": {"concurrency": settings.worker_concurrency}},
    )
    try:
        while not stop.is_set():
            try:
                await fill(database, running, settings.worker_concurrency)
            except Exception:
                # The database restarting or a deploy migrating it: try again
                # on the next poll rather than exit.
                logger.exception("pilot check worker could not claim a run")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), poll_seconds)
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        logger.info("pilot check worker stopped")
