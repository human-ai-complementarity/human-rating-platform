"""Shutdown must stop workers even when a wake lands with the cancel."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from services.assistance.runner import PreparationRunner


@pytest.mark.asyncio
async def test_close_stops_worker_woken_in_same_iteration(monkeypatch):
    runner = PreparationRunner(database=None)
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "_claim", claim)
    runner._listening.set()
    wake = runner._worker_events[0]
    worker = asyncio.create_task(runner._loop(foreground_only=True, wake=wake))
    runner._tasks = [worker]
    for _ in range(5):
        await asyncio.sleep(0)
    assert claim.await_count == 1
    try:
        # On Python 3.11, asyncio.wait_for returned the finished wait here and
        # dropped the cancel, so the worker kept polling and close() never returned.
        wake.set()
        async with asyncio.timeout(2):
            await runner.close()
        assert worker.cancelled()
        assert claim.await_count == 1
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
