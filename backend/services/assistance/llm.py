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

import openai

from config import LLMSettings

Message = dict[str, str]  # {"role": "user"|"assistant"|"system", "content": "..."}

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
                         anthropic/claude-sonnet-4.6); ignored by models that
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
    response = await client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
    return response.choices[0].message.content or ""
