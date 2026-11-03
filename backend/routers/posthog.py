"""Reverse proxy for PostHog, so ad blockers do not drop frontend events."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/e", include_in_schema=False)

API_HOST = "https://us.i.posthog.com"
ASSET_HOST = "https://us-assets.i.posthog.com"
_FORWARDED_HEADERS = ("content-type", "content-encoding", "user-agent")
_RETURNED_HEADERS = ("content-type", "cache-control")


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    return forwarded_for.split(",")[0].strip() or request.client.host


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    upstream = ASSET_HOST if path.startswith(("static/", "array/")) else API_HOST
    url = f"{upstream}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        name: request.headers[name] for name in _FORWARDED_HEADERS if name in request.headers
    }
    headers["x-forwarded-for"] = _client_ip(request)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream_response = await client.request(
                request.method, url, headers=headers, content=await request.body()
            )
    except httpx.HTTPError as exc:
        logger.warning("PostHog proxy upstream error on /%s: %s", path, type(exc).__name__)
        return Response(status_code=502)

    # httpx has already decoded the body, so Content-Encoding must not go back.
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers={
            name: upstream_response.headers[name]
            for name in _RETURNED_HEADERS
            if name in upstream_response.headers
        },
    )
