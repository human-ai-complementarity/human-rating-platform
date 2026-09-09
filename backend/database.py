from __future__ import annotations

from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from typing import Any

from fastapi import Request
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import Settings, get_settings

# An unbatched INSERT of a long-context dataset OOM-killed the production
# Postgres and restarted the whole cluster, dropping every live rater's session.
# `services.admin.question_inserts` exists to keep those statements bounded; this
# guard is what makes that mandatory rather than a convention callers remember.
#
# The trip point is deliberately loose. The batcher may emit a single row that
# exceeds its own cap (one oversized statement beats an unbounded one), so a
# tight bound would fire on correct behaviour. Anything reaching this ceiling
# isn't a large batch, it's an unbatched one.
_PAYLOAD_GUARD_HEADROOM = 8
_QUESTIONS_INSERT_PREFIX = "INSERT INTO questions"


def _payload_size(parameters: Any) -> int:
    """Approximate the string bytes a statement's parameters will carry.

    Uses `len()` rather than encoding, so this stays O(number of parameters)
    instead of copying megabytes on every insert. That undercounts non-ASCII
    text, which is fine — this distinguishes "batched" from "unbounded", where
    the two differ by orders of magnitude, not by a UTF-8 multiplier.
    """

    def scalar_size(value: Any) -> int:
        return len(value) if isinstance(value, str) else 0

    def row_size(row: Any) -> int:
        if isinstance(row, dict):
            return sum(scalar_size(value) for value in row.values())
        if isinstance(row, (list, tuple)):
            return sum(scalar_size(value) for value in row)
        return scalar_size(row)

    if not parameters:
        return 0
    if isinstance(parameters, dict):
        return row_size(parameters)
    if isinstance(parameters, (list, tuple)):
        return sum(row_size(row) for row in parameters)
    return 0


def _guard_question_insert_payload(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Fail loudly on an unbounded INSERT into `questions`.

    Raising costs one request. Letting the statement through costs the cluster.
    """
    if not statement.lstrip().startswith(_QUESTIONS_INSERT_PREFIX):
        return

    limit = get_settings().uploads.max_insert_payload_bytes * _PAYLOAD_GUARD_HEADROOM
    size = _payload_size(parameters)
    if size > limit:
        raise RuntimeError(
            f"Refusing an unbatched INSERT into questions: ~{size} bytes of parameters "
            f"exceeds the {limit} byte ceiling. Insert questions via "
            "services.admin.question_inserts.insert_questions_in_batches, which bounds "
            "each statement's payload."
        )


def register_insert_payload_guard(engine: Engine) -> None:
    """Attach the payload guard to a (synchronous) engine."""
    event.listen(engine, "before_cursor_execute", _guard_question_insert_payload)


class Database:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_maker: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        if self._engine is not None and self._session_maker is not None:
            return

        self._engine = create_async_engine(
            self._settings.async_database_url,
            pool_pre_ping=True,
        )
        register_insert_payload_guard(self._engine.sync_engine)
        self._session_maker = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )

    async def disconnect(self) -> None:
        if self._engine is None:
            return
        await self._engine.dispose()
        self._engine = None
        self._session_maker = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._session_maker is None:
            raise RuntimeError("Database is not initialized. Ensure app lifespan startup has run.")
        async with self._session_maker() as session:
            yield session


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    database: Database = request.app.state.database
    async with database.session() as session:
        yield session


def build_database(settings: Settings | None = None) -> Database:
    return Database(settings=settings or get_settings())
