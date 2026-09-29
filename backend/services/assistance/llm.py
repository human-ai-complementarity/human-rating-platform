"""Thin LLM client for assistance methods, backed by OpenRouter.

Usage:
    response = await complete(messages, settings=settings.llm)
    response = await complete(messages, model="openrouter/google/gemini-2.0-flash", settings=settings.llm)

The model string must be "openrouter/<model-id>" where model-id is any model
supported by OpenRouter (e.g. "openrouter/anthropic/claude-sonnet-4-6").
If no model is passed, settings.llm.default_model is used.
"""

from __future__ import annotations

import functools
import logging
import time
import asyncio
from contextvars import ContextVar
from weakref import WeakKeyDictionary
from contextlib import asynccontextmanager, contextmanager

import openai

from config import LLMSettings

logger = logging.getLogger(__name__)

Message = dict[str, str]  # {"role": "user"|"assistant"|"system", "content": "..."}

# Shared by foreground calls and preparation fan-out on the same event loop.
# Speculation uses at most half the slots, leaving capacity for visible work.
speculative_call: ContextVar[bool] = ContextVar("speculative_call", default=False)
_call_limits: WeakKeyDictionary = WeakKeyDictionary()
_provider_context: ContextVar[dict | None] = ContextVar("provider_context", default=None)


@contextmanager
def provider_context(**attributes):
    """Attach non-content identifiers to provider events, including fan-out tasks."""
    token = _provider_context.set({**(_provider_context.get() or {}), **attributes})
    try:
        yield
    finally:
        _provider_context.reset(token)


@asynccontextmanager
async def provider_slot():
    loop = asyncio.get_running_loop()
    if loop not in _call_limits:
        _call_limits[loop] = (asyncio.Semaphore(8), asyncio.Semaphore(4))
    total, speculative = _call_limits[loop]
    if speculative_call.get():
        async with speculative, total:
            yield
    else:
        async with total:
            yield


_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@functools.lru_cache(maxsize=4)
def _get_client(api_key: str, timeout: int, max_retries: int) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key=api_key,
        base_url=_OPENROUTER_BASE_URL,
        timeout=timeout,
        max_retries=max_retries,
    )


def _parse_model(model: str) -> str:
    """Strip the 'openrouter/' prefix and return the model-id."""
    if not model.startswith("openrouter/"):
        raise ValueError(
            f"Invalid model string {model!r}. Expected format: 'openrouter/<model-id>', "
            "e.g. 'openrouter/anthropic/claude-sonnet-4-6'."
        )
    return model.removeprefix("openrouter/")


async def complete(
    messages: list[Message],
    *,
    settings: LLMSettings,
    model: str | None = None,
    response_format: dict | None = None,
    temperature: float | None = None,
) -> str:
    """Send a chat completion request via OpenRouter and return the response text.

    Args:
        messages:        List of {"role": ..., "content": ...} dicts.
        settings:        LLMSettings instance (pass get_settings().llm).
        model:           Override the model. Defaults to settings.default_model.
                         Must be "openrouter/<model-id>".
        response_format: Optional response format dict, e.g.
                         {"type": "json_object"} or
                         {"type": "json_schema", "json_schema": {"name": "...", "schema": {...}}}.
                         Honored by OpenRouter models that advertise
                         `structured_outputs` / `response_format` (including
                         anthropic/claude-sonnet-4-6); ignored by models that
                         don't.
    """
    if not settings.openrouter_api_key:
        raise RuntimeError("LLM__OPENROUTER_API_KEY is not set.")

    model_id = _parse_model(model or settings.default_model)
    client = _get_client(
        settings.openrouter_api_key, settings.request_timeout, settings.max_retries
    )
    kwargs: dict = {"model": model_id, "messages": messages, "max_tokens": settings.max_tokens}  # type: ignore[assignment]
    if response_format is not None:
        kwargs["response_format"] = response_format
    if temperature is not None:
        kwargs["temperature"] = temperature
    started = time.monotonic()
    response = None
    outcome = "error"
    try:
        async with provider_slot():
            response = await client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
        outcome = "success" if response.choices else "empty"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        usage = getattr(response, "usage", None)
        logger.info(
            "Assistance provider call",
            extra={
                "attributes": {
                    **(_provider_context.get() or {}),
                    "prefetch.event": "provider_call",
                    "model": model_id,
                    "speculative": speculative_call.get(),
                    "outcome": outcome,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }
            },
        )
    if not response.choices:
        # OpenRouter sometimes returns HTTP 200 with an error body and no
        # choices; indexing [0] would 500 the rater's assistance fetch.
        raise RuntimeError(_empty_choices_message(response))
    return response.choices[0].message.content or ""


def _empty_choices_message(response: object) -> str:
    detail = getattr(response, "error", None)
    if detail is None:
        dump = getattr(response, "model_dump_json", None)
        if callable(dump):
            detail = dump()
    if not detail:
        return "LLM returned no choices"
    text = str(detail)
    if len(text) > 500:
        text = text[:500]
    return f"LLM returned no choices: {text}"
