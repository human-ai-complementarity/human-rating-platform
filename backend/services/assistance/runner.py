"""Bounded lifespan-owned workers backed by PostgreSQL claims.

Requests only persist demand and wait. Each worker opens short-lived sessions;
provider calls and method consumption never run inside a database transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update, case, delete
from sqlalchemy.dialects.postgresql import insert

from config import get_settings
from database import Database
from models import AssistancePreparation, AssistanceSession, Rater, QuestionAssignment

from .events import _MethodCall, _record_call
from .base import InteractionStep, StepType
from .llm import speculative_call, provider_context
from .preparation import PreparationSpec
from .registry import get_method
from .session_values import optional_json, step_columns

logger = logging.getLogger(__name__)
EXECUTION_SECONDS = 180
CLAIM_SECONDS = EXECUTION_SECONDS + 15
MAX_ATTEMPTS = 2
WAIT_SECONDS = 90
STARTUP_SECONDS = 10
SHUTDOWN_SECONDS = 5


def preparation_identity(
    rater_id: int, question_id: int, session_start: datetime, method: str, spec: PreparationSpec
):
    value = json.dumps(
        [rater_id, question_id, session_start.isoformat(), method, asdict(spec)], sort_keys=True
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot_prompt(row: AssistancePreparation) -> str | None:
    if row.context_snapshot is not None:
        return json.loads(row.context_snapshot).get("system_prompt")
    value = json.loads(row.spec_json)
    # Drain initial-step jobs created by the previous stack layer/deployment.
    return json.loads(value.get("inputs_json", "{}")).get("experiment_system_prompt")


def log_preparation_event(event: str, row: AssistancePreparation, **attributes):
    logger.info(
        "Assistance preparation",
        extra={
            "attributes": {
                "prefetch.event": event,
                "preparation_id": row.id,
                "rater_id": row.rater_id,
                "question_id": row.question_id,
                "method": row.method_name,
                **attributes,
            }
        },
    )


class PreparationRunner:
    def __init__(self, database: Database):
        self.database = database
        self._tasks: list[asyncio.Task] = []
        self._worker_events = [asyncio.Event() for _ in range(get_settings().prefetch.worker_count)]
        self._owned_tokens: set[str] = set()
        self._listening = asyncio.Event()
        self._waiters: dict[int, set[asyncio.Event]] = {}
        self._next_cleanup = 0.0

    def start(self):
        # One worker cannot be occupied by speculative work. Database claims
        # provide cross-process correctness; this limit is per API process.
        self._tasks = [asyncio.create_task(self._listen())] + [
            asyncio.create_task(self._loop(foreground_only=index == 0, wake=wake))
            for index, wake in enumerate(self._worker_events)
        ]

    @property
    def ready(self) -> bool:
        return (
            self._listening.is_set()
            and bool(self._tasks)
            and all(not task.done() for task in self._tasks)
        )

    async def wait_ready(self):
        try:
            await asyncio.wait_for(self._listening.wait(), STARTUP_SECONDS)
        except TimeoutError as exc:
            raise RuntimeError(
                "Assistance listener did not become ready before startup timeout"
            ) from exc

    def _connection_lost(self):
        self._listening.clear()
        for waiters in self._waiters.values():
            for waiter in waiters:
                waiter.set()

    def _wake_workers(self):
        for wake in self._worker_events:
            wake.set()

    def _notification(self, connection, pid, channel, payload):
        identifier, status = payload.split(":", 1)
        if status in {"complete", "cancelled", "deleted"}:
            for waiter in self._waiters.get(int(identifier), ()):
                waiter.set()
        if status in {"queued", "ready", "failed"}:
            self._wake_workers()

    async def _listen(self):
        # One connection per API process, shared by all waiting requests.
        # Notifications are hints; durable rows remain the source of truth.
        while True:
            try:
                async with self.database.notification_connection() as connection:
                    disconnected = asyncio.Event()

                    def terminated(_):
                        self._connection_lost()
                        disconnected.set()

                    connection.add_termination_listener(terminated)
                    await connection.add_listener("assistance_preparation", self._notification)
                    self._listening.set()
                    # Re-read after LISTEN commits, including reconnects. This
                    # closes the subscribe/read race and catches missed signals.
                    for waiters in self._waiters.values():
                        for waiter in waiters:
                            waiter.set()
                    self._wake_workers()
                    await disconnected.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Preparation listener disconnected; reconnecting")
            finally:
                self._connection_lost()
            await asyncio.sleep(1)

    async def close(self):
        self._connection_lost()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if not self._owned_tokens:
            return
        try:
            # Cancel computation first. Each token is an exact claim identity;
            # an expired claim taken by another process must remain untouched.
            async with asyncio.timeout(SHUTDOWN_SECONDS):
                async with self.database.session() as db:
                    await db.execute(
                        update(AssistancePreparation)
                        .where(
                            AssistancePreparation.owner_token.in_(self._owned_tokens),
                            AssistancePreparation.status.in_(["preparing", "consuming"]),
                        )
                        .values(
                            status=case(
                                (AssistancePreparation.artifact_json.is_not(None), "ready"),
                                else_="queued",
                            ),
                            owner_token=None,
                            claim_expires_at=None,
                            updated_at=datetime.now(UTC),
                        )
                    )
                    await db.commit()
        except Exception:
            logger.exception("Could not release shutdown claims; leases will recover them")
        finally:
            self._owned_tokens.clear()

    async def ensure(
        self,
        *,
        rater_id: int,
        question_id: int,
        session_start: datetime,
        method_name: str,
        spec: PreparationSpec,
        params: dict,
        deadline_at: datetime,
        demanded: bool,
        system_prompt: str | None = None,
        assignment_id: int | None = None,
        assignment_generation: int | None = None,
    ) -> int:
        identity = preparation_identity(rater_id, question_id, session_start, method_name, spec)
        identity = hashlib.sha256(
            f"{identity}:{assignment_id}:{assignment_generation}".encode()
        ).hexdigest()
        now = datetime.now(UTC)
        async with self.database.session() as db:
            values = dict(
                identity=identity,
                assignment_id=assignment_id,
                assignment_generation=assignment_generation,
                rater_id=rater_id,
                question_id=question_id,
                session_start=session_start,
                method_name=method_name,
                spec_json=json.dumps(asdict(spec)),
                context_snapshot=json.dumps({"system_prompt": system_prompt}),
                params_json=json.dumps(params),
                status="queued",
                demanded=demanded,
                deadline_at=deadline_at,
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            created = (
                await db.execute(
                    insert(AssistancePreparation)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[AssistancePreparation.identity])
                    .returning(AssistancePreparation.id)
                )
            ).scalar_one_or_none() is not None
            row = (
                await db.execute(
                    select(AssistancePreparation)
                    .where(AssistancePreparation.identity == identity)
                    .with_for_update()
                )
            ).scalar_one()
            if demanded and (created or not row.demanded):
                log_preparation_event("demand", row, ready=row.status == "ready")
            elif created:
                log_preparation_event("scheduled", row)
            if demanded:
                if row.status == "cancelled":
                    row.status = "ready" if row.artifact_json is not None else "queued"
                row.demanded = True
                row.deadline_at = deadline_at
            await db.commit()
            identifier = row.id
        self._wake_workers()
        return identifier

    async def wait(self, identifier: int) -> AssistanceSession:
        # The browser can retry after this bounded wait. Disconnecting a waiter
        # does not cancel a claim or turn a transport retry into new provider work.
        started = time.monotonic()
        stop = asyncio.get_running_loop().time() + WAIT_SECONDS
        wake = asyncio.Event()
        self._waiters.setdefault(identifier, set()).add(wake)
        try:
            async with asyncio.timeout_at(stop):
                while True:
                    wake.clear()
                    if not self._listening.is_set():
                        raise HTTPException(503, "Assistance listener unavailable; retry shortly")
                    async with self.database.session() as db:
                        row = await db.get(AssistancePreparation, identifier)
                        if row is None or row.status == "cancelled":
                            raise HTTPException(409, "Preparation is no longer valid")
                        if row.status == "complete":
                            session = (
                                await db.execute(
                                    select(AssistanceSession).where(
                                        AssistanceSession.rater_id == row.rater_id,
                                        AssistanceSession.question_id == row.question_id,
                                    )
                                )
                            ).scalar_one_or_none()
                            if session is not None:
                                log_preparation_event(
                                    "server_wait",
                                    row,
                                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                                    outcome=session.step_type,
                                    assistance_outcome=session.outcome,
                                )
                                return session
                            raise HTTPException(409, "Assistance session was reset")
                    await wake.wait()
        except TimeoutError:
            raise HTTPException(503, "Assistance is still preparing; retry the same question")
        finally:
            self._waiters[identifier].discard(wake)
            if not self._waiters[identifier]:
                del self._waiters[identifier]

    async def _loop(self, *, foreground_only: bool, wake: asyncio.Event):
        await self._listening.wait()
        while True:
            # Clear before inspecting durable state, so a concurrent commit
            # cannot be swallowed between the empty claim and wait.
            wake.clear()
            try:
                if foreground_only and time.monotonic() >= self._next_cleanup:
                    self._next_cleanup = time.monotonic() + 60
                    await self.cleanup()
                row = await self._claim(foreground_only=foreground_only)
                if row is not None:
                    await self._execute(row)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Preparation worker failed; claim will recover after expiry")
            try:
                # A process-level recovery sweep finds claims whose owner died.
                # Foreground requests never poll or hold a database connection.
                await asyncio.wait_for(wake.wait(), timeout=30)
            except TimeoutError:
                pass

    async def cleanup(self) -> int:
        """Bounded retention, after every possible session/token has expired."""
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        async with self.database.session() as db:
            rows = list(
                (
                    await db.execute(
                        select(AssistancePreparation)
                        .where(AssistancePreparation.deadline_at < cutoff)
                        .order_by(AssistancePreparation.id)
                        .with_for_update(skip_locked=True)
                        .limit(500)
                    )
                ).scalars()
            )
            if not rows:
                return 0
            await db.execute(
                delete(AssistancePreparation).where(
                    AssistancePreparation.id.in_([row.id for row in rows])
                )
            )
            await db.commit()
            for row in rows:
                log_preparation_event(
                    "retired",
                    row,
                    unused=not row.demanded and (row.artifact_json is not None or row.attempts > 0),
                    previous_status=row.status,
                )
            return len(rows)

    async def _claim(self, *, foreground_only: bool) -> AssistancePreparation | None:
        now = datetime.now(UTC)
        async with self.database.session() as db:
            query = select(AssistancePreparation).where(
                or_(
                    AssistancePreparation.status == "queued",
                    and_(
                        AssistancePreparation.status.in_(["ready", "failed"]),
                        AssistancePreparation.demanded.is_(True),
                    ),
                    and_(
                        AssistancePreparation.status.in_(["preparing", "consuming"]),
                        AssistancePreparation.claim_expires_at < now,
                    ),
                )
            )
            if foreground_only:
                query = query.where(AssistancePreparation.demanded.is_(True))
            row = (
                await db.execute(
                    query.order_by(AssistancePreparation.demanded.desc(), AssistancePreparation.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            rater = await db.get(Rater, row.rater_id)
            if not self._valid(row, rater, now) or not await self._assignment_valid(row, db, now):
                row.status = "cancelled"
                await db.commit()
                # Invalid rows are not an empty queue. Keep draining without
                # waiting for another producer or the crash-recovery sweep.
                self._wake_workers()
                return None
            row.owner_token = uuid4().hex
            # Track before commit: shutdown may interrupt the commit response.
            self._owned_tokens.add(row.owner_token)
            row.claim_expires_at = now + timedelta(seconds=CLAIM_SECONDS)
            row.status = "consuming" if row.artifact_json is not None else "preparing"
            row.attempts += 1
            row.updated_at = now
            await db.commit()
            return row

    @staticmethod
    def _valid(row, rater, now):
        return (
            rater is not None
            and rater.is_active
            and rater.session_start == row.session_start
            and now < row.deadline_at
        )

    @staticmethod
    async def _assignment_valid(row, db, now):
        if row.assignment_id is None:
            return True
        assignment = await db.get(QuestionAssignment, row.assignment_id)
        if (
            assignment is None
            or assignment.generation != row.assignment_generation
            or assignment.completed_at is not None
        ):
            return False
        if row.demanded:
            return assignment.activated_at is not None
        from services.rater.queue import speculation_enabled

        rater = await db.get(Rater, row.rater_id)
        return assignment.expires_at > now and speculation_enabled(rater.experiment_id)

    async def _execute(self, row: AssistancePreparation):
        await self._execute_claim(row)
        # Interrupted/failed executions retain their token for shutdown cleanup.
        self._owned_tokens.discard(row.owner_token)

    async def _execute_claim(self, row: AssistancePreparation):
        started = time.monotonic()
        error = None
        artifact = None
        step = None
        failed = row.attempts > MAX_ATTEMPTS
        token = speculative_call.set(not row.demanded)
        try:
            if not failed:
                method = get_method(row.method_name)
                spec = PreparationSpec(**json.loads(row.spec_json))
                with provider_context(
                    preparation_id=row.id,
                    rater_id=row.rater_id,
                    question_id=row.question_id,
                    method=row.method_name,
                ):
                    async with asyncio.timeout(EXECUTION_SECONDS):
                        if row.status == "preparing":
                            artifact = json.dumps(await method.prepare(spec))
                        else:
                            step = await method.consume_preparation(
                                spec, json.loads(row.artifact_json)
                            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            failed = True
            logger.exception(
                "Assistance preparation failed",
                extra={
                    "attributes": {
                        "preparation_id": row.id,
                        "method": row.method_name,
                    }
                },
            )
        finally:
            speculative_call.reset(token)
        if failed:
            step = InteractionStep(
                type=StepType.NONE, is_terminal=True, failure_reason="execution_error"
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        async with self.database.session() as db:
            # Serialize publication with reset/end, before locking the work row.
            rater = (
                await db.execute(select(Rater).where(Rater.id == row.rater_id).with_for_update())
            ).scalar_one_or_none()
            current = (
                await db.execute(
                    select(AssistancePreparation)
                    .where(AssistancePreparation.id == row.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current is None or current.owner_token != row.owner_token:
                log_preparation_event("fenced", row)
                return
            now = datetime.now(UTC)
            if current.claim_expires_at <= now:
                return  # Leave expired work recoverable by a new owner.
            current.execution_ms += elapsed_ms
            if not self._valid(current, rater, now) or not await self._assignment_valid(
                current, db, now
            ):
                current.status = "cancelled"
            elif artifact is not None:
                current.artifact_json = artifact
                current.status = "ready"
                current.attempts = 0
            elif step is not None:
                if current.demanded:
                    values = dict(
                        rater_id=current.rater_id,
                        experiment_id=rater.experiment_id,
                        question_id=current.question_id,
                        method_name=current.method_name,
                        params=optional_json(json.loads(current.params_json)),
                        turn=1,
                        context_snapshot=json.dumps({"system_prompt": _snapshot_prompt(current)}),
                        created_at=now,
                        **step_columns(step, now),
                    )
                    inserted_id = (
                        await db.execute(
                            insert(AssistanceSession)
                            .values(**values)
                            .on_conflict_do_nothing(
                                index_elements=[
                                    AssistanceSession.rater_id,
                                    AssistanceSession.question_id,
                                ]
                            )
                            .returning(AssistanceSession.id)
                        )
                    ).scalar_one_or_none()
                    if inserted_id is not None:
                        _record_call(
                            db,
                            session_id=inserted_id,
                            request={
                                "params": json.loads(current.params_json),
                                "retried_step_type": None,
                            },
                            call=_MethodCall(
                                step,
                                current.execution_ms,
                                error
                                or step.failure_reason
                                or ("Execution attempts exhausted" if failed else None),
                            ),
                        )
                    current.status = "complete"
                    # The durable assistance session now owns the visible step.
                    current.artifact_json = None
                    current.spec_json = "{}"
                    current.params_json = "{}"
                    current.context_snapshot = None
                else:
                    # A failed speculative result is terminal too. Demand uses
                    # this sentinel without paying for another provider attempt.
                    current.artifact_json = None
                    current.attempts = MAX_ATTEMPTS
                    current.status = "failed"
            current.updated_at = now
            current.owner_token = None
            current.claim_expires_at = None
            await db.commit()
            log_preparation_event(
                "execution",
                current,
                stage=row.status,
                outcome=current.status,
                assistance_outcome=step.outcome if step is not None else None,
                failed=failed,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                speculative=not row.demanded,
                claim_attempt=row.attempts,
            )
        self._wake_workers()
