"""Tests for the LLM client: provider selection by prefix and request shaping."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config import LLMSettings
from services.assistance.llm import (
    PROVIDERS,
    EmptyTruncatedReplyError,
    NoChoicesError,
    complete,
    missing_key_env_var,
    parse_model,
)

_MESSAGES = [{"role": "user", "content": "hi"}]


def _settings(**overrides) -> LLMSettings:
    values = {"openrouter_api_key": "sk-or-test", "oai_api_key": "sk-oai-test", **overrides}
    return LLMSettings(**values)


def _client(response=None) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=response
        or SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    )
    return client


async def _call(client: MagicMock, **kwargs) -> dict:
    with patch("services.assistance.llm._get_client", return_value=client) as get_client:
        await complete(_MESSAGES, settings=kwargs.pop("settings", _settings()), **kwargs)
    return {"client": get_client.call_args.args, **client.chat.completions.create.call_args.kwargs}


@pytest.mark.parametrize(
    "model", ["gpt-4o", "openrouter/", "oai/", "openai/gpt-4o", "anthropic/claude", "/gpt-4o"]
)
def test_parse_model_rejects_an_unknown_or_empty_prefix(model):
    """`openai/...` is an OpenRouter id in the pipeline's wave files, so it is
    not a provider here: a copied id must not quietly change transport."""
    with pytest.raises(ValueError, match="openrouter, oai"):
        parse_model(model)


def test_parse_model_keeps_the_rest_of_the_id_intact():
    provider, model_id = parse_model("openrouter/openai/gpt-4o")
    assert (provider, model_id) == (PROVIDERS["openrouter"], "openai/gpt-4o")
    provider, model_id = parse_model("oai/gpt-4o")
    assert (provider, model_id) == (PROVIDERS["oai"], "gpt-4o")


@pytest.mark.asyncio
async def test_openrouter_sends_exactly_what_it_always_has():
    """The pre-provider request body, byte for byte."""
    sent = await _call(
        _client(),
        model="openrouter/anthropic/claude-sonnet-4-6",
        temperature=0,
        response_format={"type": "json_object"},
    )
    assert sent["client"] == ("sk-or-test", "https://openrouter.ai/api/v1", 60, 2)
    assert sent["model"] == "anthropic/claude-sonnet-4-6"
    assert sent["messages"] == _MESSAGES
    assert sent["max_tokens"] == 4096
    assert sent["temperature"] == 0
    assert sent["response_format"] == {"type": "json_object"}
    assert "extra_body" not in sent
    assert "max_completion_tokens" not in sent and "reasoning_effort" not in sent


@pytest.mark.asyncio
async def test_openrouter_spells_effort_and_verbosity_its_way():
    sent = await _call(
        _client(), model="openrouter/openai/gpt-5", reasoning_effort="low", text_verbosity="high"
    )
    assert sent["extra_body"] == {"reasoning": {"effort": "low"}, "verbosity": "high"}
    assert "temperature" not in sent


@pytest.mark.asyncio
async def test_oai_uses_its_own_key_and_parameter_names():
    sent = await _call(
        _client(),
        model="oai/gpt-5.6-luna",
        reasoning_effort="low",
        text_verbosity="low",
        response_format={"type": "json_object"},
    )
    assert sent["client"] == ("sk-oai-test", None, 60, 2)
    assert sent["model"] == "gpt-5.6-luna"
    assert sent["max_completion_tokens"] == 4096
    assert sent["reasoning_effort"] == "low"
    assert sent["verbosity"] == "low"
    assert sent["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in sent and "extra_body" not in sent and "temperature" not in sent


@pytest.mark.asyncio
async def test_oai_sends_a_declared_temperature():
    sent = await _call(_client(), model="oai/gpt-4o", temperature=0.7)
    assert sent["temperature"] == 0.7


@pytest.mark.asyncio
async def test_the_default_model_is_used_when_none_is_passed():
    sent = await _call(_client())
    assert f"openrouter/{sent['model']}" == LLMSettings().default_model


@pytest.mark.parametrize(
    ("model", "settings", "env_var"),
    [
        ("openrouter/x/y", _settings(openrouter_api_key=""), "LLM__OPENROUTER_API_KEY"),
        ("oai/y", _settings(oai_api_key=""), "LLM__OAI_API_KEY"),
    ],
)
@pytest.mark.asyncio
async def test_a_missing_key_names_the_provider_that_needs_it(model, settings, env_var):
    assert missing_key_env_var(model, settings) == env_var
    with pytest.raises(RuntimeError, match=f"^{env_var} is not set.$"):
        await complete(_MESSAGES, model=model, settings=settings)


def test_a_present_key_is_not_missing():
    assert missing_key_env_var("oai/y", _settings()) is None


def _reply(content: str | None, finish_reason: str | None) -> SimpleNamespace:
    choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "cap"),
    [("openrouter/openai/gpt-5", "max_tokens"), ("oai/gpt-5", "max_completion_tokens")],
)
@pytest.mark.parametrize("content", [None, "", " \n"])
async def test_complete_raises_when_reply_is_cut_off_with_no_content(model, cap, content):
    """Both providers: the message names the cap the way that provider was sent it."""
    with (
        patch(
            "services.assistance.llm._get_client", return_value=_client(_reply(content, "length"))
        ),
        pytest.raises(
            EmptyTruncatedReplyError, match=f"^LLM reply from {model} hit {cap}=4096 "
        ) as info,
    ):
        await complete(_MESSAGES, model=model, settings=_settings())

    assert "reasoning" in str(info.value)
    # The methods' provider-error handlers catch RuntimeError.
    assert isinstance(info.value, RuntimeError)


@pytest.mark.asyncio
async def test_complete_returns_truncated_reply_that_has_content():
    client = _client(_reply('{"candidates": [', "length"))
    with patch("services.assistance.llm._get_client", return_value=client):
        raw = await complete(_MESSAGES, settings=_settings())

    assert raw == '{"candidates": ['


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["stop", "content_filter", None])
async def test_complete_returns_empty_reply_that_was_not_cut_off(finish_reason):
    client = _client(_reply(None, finish_reason))
    with patch("services.assistance.llm._get_client", return_value=client):
        raw = await complete(_MESSAGES, settings=_settings())

    assert raw == ""


@pytest.mark.asyncio
async def test_complete_raises_when_choices_empty():
    with (
        patch(
            "services.assistance.llm._get_client", return_value=_client(SimpleNamespace(choices=[]))
        ),
        pytest.raises(NoChoicesError, match="^LLM returned no choices$") as info,
    ):
        await complete(_MESSAGES, settings=_settings())
    assert info.value.status_code is None


@pytest.mark.asyncio
async def test_complete_includes_error_body_and_code_when_choices_empty():
    response = SimpleNamespace(choices=[], error={"message": "No endpoints found", "code": 404})
    with (
        patch("services.assistance.llm._get_client", return_value=_client(response)),
        pytest.raises(NoChoicesError, match="No endpoints found") as info,
    ):
        await complete(_MESSAGES, settings=_settings())
    assert info.value.status_code == 404
    assert isinstance(info.value, RuntimeError)
