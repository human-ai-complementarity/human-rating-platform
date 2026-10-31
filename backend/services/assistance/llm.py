"""Thin LLM client for assistance methods.

Usage:
    response = await complete(messages, settings=settings.llm)
    response = await complete(messages, model="oai/gpt-5.6-luna", settings=settings.llm)

The model string is "<provider>/<model-id>". Providers are the keys of
`PROVIDERS`: "openrouter" (any model OpenRouter serves, e.g.
"openrouter/openai/gpt-5.6-luna") and "oai" (OpenAI's own API, e.g.
"oai/gpt-5.6-luna"). The direct provider is named as the inference pipeline
names it, not "openai", because "openai/<model>" is an OpenRouter id there:
a copied id must not quietly change transport. Each provider has its own API
key setting and its own spelling for the request options; callers only ever
pass the neutral names. If no model is passed, settings.llm.default_model is
used.

Tools (`tools=["web_search"]`) are the inference arm's: what its answerer
was given, run the way the pipeline runs them. On "oai" a call with tools
goes to OpenAI's Responses API, which runs web_search and code_interpreter
itself; on "openrouter" it stays a chat completion with OpenRouter's
`openrouter:web_search` server tool, and code_interpreter is not available.
A call without tools is a chat completion on either, as it always was.
"""

from __future__ import annotations

import functools
import asyncio
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from copy import deepcopy
from weakref import WeakKeyDictionary
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

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
class Provider:
    """One transport: where to send, which key to use, how to spell the options.

    `request` builds the chat-completions kwargs. Every option is None for
    "do not send"; the provider-neutral names are spelled its way by
    `reasoning_kwargs`.
    """

    base_url: str | None
    # Attribute on LLMSettings holding the key, and the env var that sets it.
    key_setting: str
    env_var: str
    # The output cap's name: OpenAI's reasoning models refuse `max_tokens`.
    max_tokens_key: str

    # Entry tool name -> the tool as this transport's request spells it. An
    # entry on this provider may declare only these; `tool_limit` says why.
    tools: ClassVar[Mapping[str, Mapping[str, Any]]] = {}
    tool_limit: ClassVar[str] = ""
    # Whether a call with tools goes to the Responses API (`responses_request`)
    # rather than staying a chat completion.
    tools_use_responses_api: ClassVar[bool] = False

    def api_key(self, settings: LLMSettings) -> str:
        key = getattr(settings, self.key_setting)
        if not key:
            raise RuntimeError(f"{self.env_var} is not set.")
        return key

    def request(
        self,
        model_id: str,
        messages: list[Message],
        settings: LLMSettings,
        *,
        response_format: dict | None,
        temperature: float | None,
        reasoning_effort: str | None,
        text_verbosity: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            self.max_tokens_key: settings.max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if temperature is not None:
            kwargs["temperature"] = temperature
        kwargs.update(self.reasoning_kwargs(reasoning_effort, text_verbosity))
        return kwargs

    def reasoning_kwargs(self, effort: str | None, verbosity: str | None) -> dict[str, Any]:
        raise NotImplementedError

    def tool_specs(self, tools: Sequence[str]) -> list[dict[str, Any]]:
        """`tools` as this transport's request spells them."""
        return [deepcopy(dict(self.tools[tool])) for tool in tools]


class _OpenRouter(Provider):
    # The server tool the inference pipeline's answerer sends; OpenRouter
    # runs the search inside the request. Its `web` plugin and `:online`
    # suffix are deprecated in favour of it. OpenRouter runs no code.
    tools = {"web_search": {"type": "openrouter:web_search"}}
    tool_limit = "OpenRouter can only do web search; use an oai/ model for code_interpreter."

    def request(
        self,
        model_id: str,
        messages: list[Message],
        settings: LLMSettings,
        *,
        tools: Sequence[str] = (),
        **options: Any,
    ) -> dict[str, Any]:
        kwargs = super().request(model_id, messages, settings, **options)
        if tools:
            # Not the SDK's `tools`: a server tool is not an OpenAI tool type.
            kwargs.setdefault("extra_body", {})["tools"] = self.tool_specs(tools)
        return kwargs

    def reasoning_kwargs(self, effort: str | None, verbosity: str | None) -> dict[str, Any]:
        # OpenRouter drops a parameter the routed endpoint does not support
        # rather than refusing it; the launch-time preflight is where a
        # declared option gets checked, not the routing.
        extra: dict[str, Any] = {}
        if effort is not None:
            extra["reasoning"] = {"effort": effort}
        if verbosity is not None:
            extra["verbosity"] = verbosity
        return {"extra_body": extra} if extra else {}


class _OpenAI(Provider):
    # OpenAI's hosted tools, as the inference pipeline's answerer sends them.
    tools = {
        "web_search": {"type": "web_search"},
        "code_interpreter": {"type": "code_interpreter", "container": {"type": "auto"}},
    }
    tools_use_responses_api = True

    def reasoning_kwargs(self, effort: str | None, verbosity: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if effort is not None:
            kwargs["reasoning_effort"] = effort
        if verbosity is not None:
            kwargs["verbosity"] = verbosity
        return kwargs

    def responses_request(
        self,
        model_id: str,
        messages: list[Message],
        settings: LLMSettings,
        *,
        tools: Sequence[str],
        response_format: dict | None,
        temperature: float | None,
        reasoning_effort: str | None,
        text_verbosity: str | None,
    ) -> dict[str, Any]:
        """The Responses API kwargs: the same call as `request`, with tools.

        The chat messages are the input as they are; the output cap is the
        same setting; a chat `response_format` becomes `text.format`.
        """
        kwargs: dict[str, Any] = {
            "model": model_id,
            "input": messages,
            "tools": self.tool_specs(tools),
            "max_output_tokens": settings.max_tokens,
            # Not kept on OpenAI's side, as a chat completion is not.
            "store": False,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": reasoning_effort}
        text: dict[str, Any] = {}
        if text_verbosity is not None:
            text["verbosity"] = text_verbosity
        if response_format is not None:
            text["format"] = _text_format(response_format)
        if text:
            kwargs["text"] = text
        return kwargs


def _text_format(response_format: dict) -> dict:
    """A chat `response_format` as a Responses `text.format`: the json_schema
    fields sit beside its type rather than under `json_schema`."""
    if response_format.get("type") == "json_schema":
        return {"type": "json_schema", **response_format["json_schema"]}
    return dict(response_format)


PROVIDERS: dict[str, Provider] = {
    "openrouter": _OpenRouter(
        base_url="https://openrouter.ai/api/v1",
        key_setting="openrouter_api_key",
        env_var="LLM__OPENROUTER_API_KEY",
        max_tokens_key="max_tokens",
    ),
    "oai": _OpenAI(
        base_url=None,
        key_setting="oai_api_key",
        env_var="LLM__OAI_API_KEY",
        max_tokens_key="max_completion_tokens",
    ),
}


class NoChoicesError(RuntimeError):
    """The provider answered 200 with no choices, usually an error body.

    OpenRouter reports "no endpoints found" for a model/parameter combination
    this way rather than as an HTTP error; `status_code` carries the code from
    that body when there is one, so callers can treat it like a 4xx.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EmptyTruncatedReplyError(RuntimeError):
    """The reply was cut off at the output cap before any content was written."""


class FailedResponseError(RuntimeError):
    """A Responses API call came back failed, or stopped early with no text
    for a reason other than the output cap (e.g. a content filter)."""


@functools.lru_cache(maxsize=8)
def _get_client(
    api_key: str, base_url: str | None, timeout: int, max_retries: int
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


def missing_key_env_var(model: str, settings: LLMSettings) -> str | None:
    """The env var that must be set before `model` can be called, if it is not."""
    provider, _ = parse_model(model)
    return None if getattr(settings, provider.key_setting) else provider.env_var


async def complete(
    messages: list[Message],
    *,
    settings: LLMSettings,
    model: str | None = None,
    response_format: dict | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    text_verbosity: str | None = None,
    tools: Sequence[str] = (),
) -> str:
    """Send a chat completion request (with tools on "oai", a Responses API
    request) and return the response text.

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
        tools:            Tools the model may use, from the provider's `tools`.
                          With any, an "oai" call goes to the Responses API.
    """
    name = model or settings.default_model
    provider, model_id = parse_model(name)
    unsupported = [tool for tool in tools if tool not in provider.tools]
    if unsupported:
        # Validation refuses these at write; never run the entry without them.
        raise ValueError(
            f"{name} cannot use tool(s) {', '.join(unsupported)}. {provider.tool_limit}".strip()
        )
    # A call with tools runs them before it answers, so it waits longer, and
    # only once: a timed-out tool loop retried mostly times out again, paid
    # again, and the retries would outlast a preparation's EXECUTION_SECONDS.
    timeout, retries = (
        (settings.tool_request_timeout, 0)
        if tools
        else (settings.request_timeout, settings.max_retries)
    )
    client = _get_client(provider.api_key(settings), provider.base_url, timeout, retries)
    options = dict(
        response_format=response_format,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        text_verbosity=text_verbosity,
    )
    if tools and provider.tools_use_responses_api:
        kwargs = provider.responses_request(model_id, messages, settings, tools=tools, **options)
        async with provider_slot():
            response = await client.responses.create(**kwargs)
        return _response_text(response, name, settings)
    if tools:
        options["tools"] = tools
    kwargs = provider.request(model_id, messages, settings, **options)
    async with provider_slot():
        response = await client.chat.completions.create(**kwargs)
    if not response.choices:
        # OpenRouter sometimes returns HTTP 200 with an error body and no
        # choices; indexing [0] would 500 the rater's assistance fetch.
        raise NoChoicesError(_empty_choices_message(response), _error_status_code(response))
    choice = response.choices[0]
    content = choice.message.content or ""
    if not content.strip() and choice.finish_reason == "length":
        # A reasoning model can spend the whole output cap on hidden reasoning.
        # Raise so callers record a provider error, not an unparseable reply.
        raise EmptyTruncatedReplyError(
            f"LLM reply from {name} hit "
            f"{provider.max_tokens_key}={settings.max_tokens} with no content; "
            "reasoning may have used the whole budget"
        )
    return content


def _response_text(response: Any, model: str, settings: LLMSettings) -> str:
    """The reply in a Responses API result: its last message's text.

    With tools the output also holds the tool calls, and a model may write a
    message before a call as well as after it; the last message is the
    answer, as the inference pipeline reads it.
    """
    if response.status == "failed" or response.error is not None:
        error = response.error
        detail = f"{error.code}: {error.message}" if error is not None else "no error given"
        raise FailedResponseError(f"LLM response from {model} failed: {detail}")
    messages = [item for item in response.output if item.type == "message"]
    content = (
        "".join(part.text for part in messages[-1].content if part.type == "output_text")
        if messages
        else ""
    )
    ended_on_message = bool(response.output) and response.output[-1].type == "message"
    if response.status == "incomplete" and not ended_on_message:
        # Cut off after its last message, mid tool call or reasoning: that
        # message came before the answer, so there is none.
        content = ""
    if content.strip() or response.status != "incomplete":
        return content
    details = response.incomplete_details
    reason = details.reason if details is not None else None
    if reason == "max_output_tokens":
        # As on chat completions: reasoning (and here tool calls) can spend
        # the whole output cap. Raise so callers record a provider error.
        raise EmptyTruncatedReplyError(
            f"LLM reply from {model} hit max_output_tokens={settings.max_tokens} with no "
            "content; reasoning or tool calls may have used the whole budget"
        )
    raise FailedResponseError(f"LLM response from {model} is incomplete ({reason}) with no content")


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
