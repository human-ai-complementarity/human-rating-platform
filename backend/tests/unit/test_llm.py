"""Tests for the OpenRouter LLM client."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config import LLMSettings
from services.assistance.llm import complete


def _settings() -> LLMSettings:
    return LLMSettings(openrouter_api_key="sk-test")


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
