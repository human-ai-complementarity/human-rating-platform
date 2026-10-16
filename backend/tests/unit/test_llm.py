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


@pytest.mark.asyncio
async def test_provider_events_keep_concurrent_contexts_separate(caplog):
    import asyncio
    import logging
    from services.assistance.llm import provider_context

    mock_client = MagicMock()

    async def create(**kwargs):
        await asyncio.sleep(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(total_tokens=12),
        )

    mock_client.chat.completions.create = create
    caplog.set_level(logging.INFO, logger="services.assistance.llm")

    async def request(identifier):
        with provider_context(rater_id=identifier, method="example", preparation_id=identifier):
            # Task creation, as used by method fan-out, retains its own context.
            await asyncio.create_task(
                complete([{"role": "user", "content": "private prompt"}], settings=_settings())
            )

    with patch("services.assistance.llm._get_client", return_value=mock_client):
        await asyncio.gather(request(1), request(2))
        await complete([], settings=_settings())
    events = [
        record.attributes
        for record in caplog.records
        if getattr(record, "attributes", {}).get("prefetch.event") == "provider_call"
    ]
    assert {event["rater_id"] for event in events[:2]} == {1, 2}
    assert all(
        event["preparation_id"] == event["rater_id"] and event["method"] == "example"
        for event in events[:2]
    )
    assert "rater_id" not in events[2]
    assert "private prompt" not in str(events)
