"""Tests for the OpenRouter LLM client."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config import LLMSettings
from services.assistance.llm import complete


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _settings() -> LLMSettings:
    return LLMSettings(openrouter_api_key="sk-test")


@pytest.mark.asyncio
async def test_complete_raises_when_choices_empty():
    mock_response = MagicMock()
    mock_response.choices = []
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with (
        patch("services.assistance.llm._get_client", return_value=mock_client),
        pytest.raises(RuntimeError, match="no choices"),
    ):
        await complete([{"role": "user", "content": "hi"}], settings=_settings())
