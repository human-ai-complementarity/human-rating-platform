"""Shared assistance call history for requests and durable publication."""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from models import AssistanceEvent
from services.queries import load_json_column
from .base import InteractionStep, StepType, exception_text

logger = logging.getLogger(__name__)


@dataclass
class _MethodCall:
    """Outcome of one start()/advance() invocation, as the event log records it."""

    step: InteractionStep
    latency_ms: int
    # Exception text or the method's failure_reason; None when the call succeeded.
    error: str | None


async def _call_method(
    invoke: Callable[[], Awaitable[InteractionStep]],
    *,
    fallback: StepType,
    log_message: str,
    log_attributes: dict,
) -> _MethodCall:
    """Run a method call, timing it and degrading any failure to ``fallback``.

    RuntimeError is the methods' documented "give up" signal, but anything
    else escaping a method (a KeyError on corrupt state, a provider exception
    the method forgot to catch) is just as unrecoverable from here, and a 500
    would roll back the event row that is supposed to explain it. So every
    exception degrades to the fallback step and is logged with its traceback.
    """
    started = time.monotonic()
    try:
        step = await invoke()
    except Exception as exc:
        latency_ms = _elapsed_ms(started)
        logger.error(log_message, exc_info=True, extra={"attributes": log_attributes})
        return _MethodCall(
            step=InteractionStep(type=fallback, is_terminal=True),
            latency_ms=latency_ms,
            error=exception_text(exc),
        )
    latency_ms = _elapsed_ms(started)
    # A set failure_reason means the method caught its own failure and
    # returned a degraded step.
    return _MethodCall(step, latency_ms, step.error_text())


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _record_call(db: AsyncSession, *, session_id: int, request: dict, call: _MethodCall) -> None:
    """Append the one event row for a start()/advance() call."""
    response: dict = {
        "payload": call.step.payload,
        "state": call.step.state,
        "is_terminal": call.step.is_terminal,
    }
    if call.step.failure_reason:
        response["failure_reason"] = call.step.failure_reason
    db.add(
        AssistanceEvent(
            assistance_session_id=session_id,
            step_type=call.step.type.value,
            latency_ms=call.latency_ms,
            payload=json.dumps({"request": request, "response": response}),
            error=call.error,
        )
    )


async def _last_human_input(session_id: int, db: AsyncSession) -> str | None:
    """The ``human_input`` of the session's most recent call, if it was an advance."""
    payload = (
        await db.execute(
            select(AssistanceEvent.payload)
            .where(AssistanceEvent.assistance_session_id == session_id)
            .order_by(AssistanceEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return load_json_column(payload).get("request", {}).get("human_input")
