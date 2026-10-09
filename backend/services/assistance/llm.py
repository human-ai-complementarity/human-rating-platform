"""Thin LLM client for assistance methods.

Usage:
    response = await complete(messages, settings=settings.llm)
    response = await complete(messages, model="openai/gpt-5.6-luna", settings=settings.llm)

The model string is "<provider>/<model-id>". Providers are the keys of
`PROVIDERS`: "openrouter" (any model OpenRouter serves, e.g.
"openrouter/anthropic/claude-sonnet-4-6") and "openai" (OpenAI's own API,
e.g. "openai/gpt-4o"). Each provider has its own API key setting and its own
spelling for the request options; callers only ever pass the neutral names.
If no model is passed, settings.llm.default_model is used.
"""

from __future__ import annotations

import functools
import asyncio
from contextvars import ContextVar
from weakref import WeakKeyDictionary
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import openai

from config import LLMSettings

Message = dict[str, str]  # {"role": "user"|"assistant"|"system", "content": "..."}

# Shared by foreground calls and preparation fan-out on the same event loop.
# Speculation uses at most half the slots, leaving capacity for visible work.
speculative_call: ContextVar[bool] = ContextVar("speculative_call", default=False)
_call_limits: WeakKeyDictionary = WeakKeyDictionary()


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


# The values a wave may declare, mirrored from the inference pipeline's
# `ReasoningEffort` / `TextVerbosity` so an arm copies over verbatim.
REASONING_EFFORTS = ("minimal", "low", "medium", "high")
TEXT_VERBOSITIES = ("low", "medium", "high")


@dataclass(frozen=True)
class RequestOptions:
    """Provider-neutral knobs on one completion. None means "do not send"."""

    response_format: dict | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    text_verbosity: str | None = None


@dataclass(frozen=True)
class Provider:
    """One transport: where to send, which key to use, how to spell the options."""

    name: str
    base_url: str | None
    # Attribute on LLMSettings holding the key, and the env var that sets it.
    key_setting: str
    env_var: str

    def api_key(self, settings: LLMSettings) -> str:
        key = getattr(settings, self.key_setting)
        if not key:
            raise RuntimeError(f"{self.env_var} is not set.")
        return key

    def request(
        self, model_id: str, messages: list[Message], settings: LLMSettings, options: RequestOptions
    ) -> dict[str, Any]:
        raise NotImplementedError


class _OpenRouter(Provider):
    def request(
        self, model_id: str, messages: list[Message], settings: LLMSettings, options: RequestOptions
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "max_tokens": settings.max_tokens,
        }
        if options.response_format is not None:
            kwargs["response_format"] = options.response_format
        if options.temperature is not None:
            kwargs["temperature"] = options.temperature
        # OpenRouter's default is to drop a parameter the routed endpoint does
        # not support, so a declared temperature or effort could silently not
        # run. `require_parameters` makes it refuse instead, the way OpenAI
        # does, so both transports fail the same way on the same declaration.
        extra: dict[str, Any] = {"provider": {"require_parameters": True}}
        if options.reasoning_effort is not None:
            extra["reasoning"] = {"effort": options.reasoning_effort}
        if options.text_verbosity is not None:
            extra["verbosity"] = options.text_verbosity
        kwargs["extra_body"] = extra
        return kwargs


class _OpenAI(Provider):
    def request(
        self, model_id: str, messages: list[Message], settings: LLMSettings, options: RequestOptions
    ) -> dict[str, Any]:
        # `max_tokens` is refused by OpenAI's reasoning models; the renamed
        # field is accepted by every chat model.
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "max_completion_tokens": settings.max_tokens,
        }
        if options.response_format is not None:
            kwargs["response_format"] = options.response_format
        if options.temperature is not None:
            kwargs["temperature"] = options.temperature
        if options.reasoning_effort is not None:
            kwargs["reasoning_effort"] = options.reasoning_effort
        if options.text_verbosity is not None:
            kwargs["verbosity"] = options.text_verbosity
        return kwargs


PROVIDERS: dict[str, Provider] = {
    "openrouter": _OpenRouter(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        key_setting="openrouter_api_key",
        env_var="LLM__OPENROUTER_API_KEY",
    ),
    "openai": _OpenAI(
        name="openai",
        base_url=None,
        key_setting="openai_api_key",
        env_var="LLM__OPENAI_API_KEY",
    ),
}


def model_prefixes() -> tuple[str, ...]:
    """The prefixes a model id may start with, e.g. ("openrouter/", "openai/")."""
    return tuple(f"{name}/" for name in PROVIDERS)


class NoChoicesError(RuntimeError):
    """The provider answered 200 with no choices, usually an error body.

    OpenRouter reports "no endpoints found" for a model/parameter combination
    this way rather than as an HTTP error; `status_code` carries the code from
    that body when there is one, so callers can treat it like a 4xx.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@functools.lru_cache(maxsize=8)
def _get_client(
    provider: str, api_key: str, base_url: str | None, timeout: int, max_retries: int
) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


def parse_model(model: str) -> tuple[Provider, str]:
    """Split "<provider>/<model-id>" into its provider and the id it serves."""
    prefix, _, model_id = model.partition("/")
    provider = PROVIDERS.get(prefix)
    if provider is None or not model_id:
        raise ValueError(
            f"Invalid model string {model!r}. Expected format: '<provider>/<model-id>' with "
            f"provider one of {', '.join(PROVIDERS)}, e.g. 'openrouter/anthropic/claude-sonnet-4-6'."
        )
    return provider, model_id


async def complete(
    messages: list[Message],
    *,
    settings: LLMSettings,
    model: str | None = None,
    response_format: dict | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    text_verbosity: str | None = None,
) -> str:
    """Send a chat completion request and return the response text.

    Args:
        messages:         List of {"role": ..., "content": ...} dicts.
        settings:         LLMSettings instance (pass get_settings().llm).
        model:            Override the model. Defaults to settings.default_model.
                          Must be "<provider>/<model-id>".
        response_format:  Optional response format dict, e.g.
                          {"type": "json_object"} or
                          {"type": "json_schema", "json_schema": {"name": "...", "schema": {...}}}.
        temperature:      Sent when not None.
        reasoning_effort: One of REASONING_EFFORTS; sent when not None.
        text_verbosity:   One of TEXT_VERBOSITIES; sent when not None.
    """
    provider, model_id = parse_model(model or settings.default_model)
    client = _get_client(
        provider.name,
        provider.api_key(settings),
        provider.base_url,
        settings.request_timeout,
        settings.max_retries,
    )
    options = RequestOptions(
        response_format=response_format,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        text_verbosity=text_verbosity,
    )
    kwargs = provider.request(model_id, messages, settings, options)
    async with provider_slot():
        response = await client.chat.completions.create(**kwargs)
    if not response.choices:
        # OpenRouter sometimes returns HTTP 200 with an error body and no
        # choices; indexing [0] would 500 the rater's assistance fetch.
        raise NoChoicesError(_empty_choices_message(response), _error_status_code(response))
    return response.choices[0].message.content or ""


def _error_status_code(response: object) -> int | None:
    detail = getattr(response, "error", None)
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, int) else None


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
