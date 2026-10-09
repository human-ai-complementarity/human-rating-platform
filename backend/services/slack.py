"""Minimal Slack incoming-webhook client."""

from __future__ import annotations

import httpx

from config import SlackSettings


class SlackWebhookError(Exception):
    pass


def escape_mrkdwn(text: str) -> str:
    """Escape the characters Slack treats as markup, so participant text can't
    render as a link or an `<!channel>` mention."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def post_webhook_message(*, settings: SlackSettings, text: str) -> None:
    if not settings.enabled:
        raise RuntimeError("post_webhook_message called while Slack is disabled")

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(settings.webhook_url, json={"text": text})
    # Not `raise_for_status`: its message carries the URL, which is the credential.
    if not response.is_success:
        raise SlackWebhookError(f"Slack webhook returned {response.status_code}")
