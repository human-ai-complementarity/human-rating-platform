"""The /e route forwards PostHog traffic upstream with the real client IP."""

from __future__ import annotations

import respx
from fastapi.testclient import TestClient
from httpx import ConnectError, Response

from routers.posthog import API_HOST, ASSET_HOST


@respx.mock
def test_event_post_reaches_api_host_with_client_ip(client: TestClient):
    route = respx.post(f"{API_HOST}/i/v0/e/").mock(return_value=Response(200, json={"status": 1}))
    body = b'{"event":"$pageview"}'

    response = client.post(
        "/e/i/v0/e/?ip=0&compression=gzip-js",
        content=body,
        headers={
            "Content-Type": "text/plain",
            "User-Agent": "rater-browser/1.0",
            "X-Forwarded-For": "203.0.113.7, 10.0.0.1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": 1}
    sent = route.calls.last.request
    assert sent.method == "POST"
    assert sent.url.path == "/i/v0/e/"
    assert sent.url.query == b"ip=0&compression=gzip-js"
    assert sent.content == body
    assert sent.headers["host"] == "us.i.posthog.com"
    assert sent.headers["x-forwarded-for"] == "203.0.113.7"
    assert sent.headers["user-agent"] == "rater-browser/1.0"


@respx.mock
def test_static_assets_go_to_asset_host(client: TestClient):
    route = respx.get(f"{ASSET_HOST}/static/array.js").mock(
        return_value=Response(
            200,
            text="//js",
            headers={"Content-Type": "application/javascript", "Cache-Control": "max-age=3600"},
        )
    )

    response = client.get("/e/static/array.js")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/javascript"
    assert response.headers["cache-control"] == "max-age=3600"
    assert route.calls.last.request.headers["host"] == "us-assets.i.posthog.com"


@respx.mock
def test_upstream_failure_is_bad_gateway(client: TestClient):
    respx.post(f"{API_HOST}/i/v0/e/").mock(side_effect=ConnectError)

    assert client.post("/e/i/v0/e/", content=b"{}").status_code == 502
