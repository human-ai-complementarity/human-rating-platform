"""Shutdown must stop workers and the listener even when their cancel is swallowed."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from services.assistance.runner import PreparationRunner


@pytest.mark.asyncio
async def test_close_stops_worker_woken_in_same_iteration(monkeypatch):
    runner = PreparationRunner(database=None)
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "_claim", claim)
    monkeypatch.setattr(runner, "cleanup", AsyncMock(return_value=0))
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


class _Connection:
    def add_termination_listener(self, callback):
        pass

    async def add_listener(self, channel, callback):
        pass


class _NotificationDatabase:
    def __init__(self):
        self.connects = 0

    @asynccontextmanager
    async def notification_connection(self):
        self.connects += 1
        first = self.connects == 1
        try:
            yield _Connection()
        finally:
            # Only the first close fails, so a regression reconnects once and
            # the test's timeout can still cancel it instead of hanging.
            if first:
                raise ConnectionResetError("connection lost while closing")


@pytest.mark.asyncio
async def test_close_stops_listener_when_connection_close_fails():
    database = _NotificationDatabase()
    runner = PreparationRunner(database=database)
    listener = asyncio.create_task(runner._listen())
    runner._tasks = [listener]
    try:
        async with asyncio.timeout(2):
            await runner._listening.wait()
        # The close error replaced the cancel, so the listener logged a
        # disconnect, reconnected and close() never returned.
        async with asyncio.timeout(3):
            await runner.close()
        assert listener.cancelled()
        assert database.connects == 1
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
