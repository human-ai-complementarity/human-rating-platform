"""Tests for the OpenRouter LLM client."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config import LLMSettings
from services.assistance.llm import EmptyTruncatedReplyError, complete


def _settings() -> LLMSettings:
    return LLMSettings(openrouter_api_key="sk-test")


def _client_returning(content: str | None, finish_reason: str | None) -> MagicMock:
    choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[choice]))
    return mock_client


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", " \n"])
async def test_complete_raises_when_reply_is_cut_off_with_no_content(content):
    with (
        patch(
            "services.assistance.llm._get_client",
            return_value=_client_returning(content, "length"),
        ),
        pytest.raises(EmptyTruncatedReplyError, match="max_tokens=4096") as excinfo,
    ):
        await complete([{"role": "user", "content": "hi"}], settings=_settings())

    assert "reasoning" in str(excinfo.value)
    # The methods' provider-error handlers catch RuntimeError.
    assert isinstance(excinfo.value, RuntimeError)


@pytest.mark.asyncio
async def test_complete_returns_truncated_reply_that_has_content():
    with patch(
        "services.assistance.llm._get_client",
        return_value=_client_returning('{"candidates": [', "length"),
    ):
        raw = await complete([{"role": "user", "content": "hi"}], settings=_settings())

    assert raw == '{"candidates": ['


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["stop", "content_filter", None])
async def test_complete_returns_empty_reply_that_was_not_cut_off(finish_reason):
    with patch(
        "services.assistance.llm._get_client",
        return_value=_client_returning(None, finish_reason),
    ):
        raw = await complete([{"role": "user", "content": "hi"}], settings=_settings())

    assert raw == ""


@pytest.mark.asyncio
async def test_complete_raises_when_choices_empty():
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[]))

    with (
        patch("services.assistance.llm._get_client", return_value=mock_client),
        pytest.raises(RuntimeError, match="^LLM returned no choices$"),
    ):
        await complete([{"role": "user", "content": "hi"}], settings=_settings())


@pytest.mark.asyncio
async def test_complete_includes_error_body_when_choices_empty():
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(choices=[], error={"message": "No endpoints found"})
    )

    with (
        patch("services.assistance.llm._get_client", return_value=mock_client),
        pytest.raises(RuntimeError, match="No endpoints found"),
    ):
        await complete([{"role": "user", "content": "hi"}], settings=_settings())
