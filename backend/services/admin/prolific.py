"""Prolific API client for automated study management.

All Prolific HTTP calls live here. The service is stateless -- it receives
the API token and base URL from config, and is only called when Prolific
integration is enabled.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import string

import httpx

from config import ProlificSettings

logger = logging.getLogger(__name__)

COMPLETION_CODE_LENGTH = 8
COMPLETION_URL_TEMPLATE = "https://app.prolific.com/submissions/complete?cc={code}"
REAL_STUDY_URL_TEMPLATE = "https://app.prolific.com/researcher/workspaces/studies/{study_id}"

# Prolific only supports USD and GBP. If they add more, extend this map; an
# unknown code falls back to displaying the code itself in place of a symbol.
CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "GBP": "£",
}


def generate_completion_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(COMPLETION_CODE_LENGTH))


def build_completion_url(code: str) -> str:
    return COMPLETION_URL_TEMPLATE.format(code=code)


def build_external_study_url(*, site_url: str, experiment_id: int) -> str:
    return (
        f"{site_url}/rate"
        f"?experiment_id={experiment_id}"
        f"&PROLIFIC_PID={{{{%PROLIFIC_PID%}}}}"
        f"&STUDY_ID={{{{%STUDY_ID%}}}}"
        f"&SESSION_ID={{{{%SESSION_ID%}}}}"
    )


def build_study_url(*, study_id: str) -> str:
    return REAL_STUDY_URL_TEMPLATE.format(study_id=study_id)


def _build_client(settings: ProlificSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.base_url,
        headers={"Authorization": f"Token {settings.api_token}"},
        timeout=30.0,
    )


class ProlificAPIError(Exception):
    """Raised when Prolific returns a non-2xx HTTP status.

    Carries the response body so callers can surface Prolific's actual
    error message instead of a generic 502.
    """

    def __init__(self, status_code: int, body: str, url: str) -> None:
        self.status_code = status_code
        self.body = body
        self.url = url
        super().__init__(f"Prolific {status_code} for {url}: {body[:500]}")


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    raise ProlificAPIError(
        status_code=response.status_code,
        body=response.text,
        url=str(response.request.url) if response.request else "",
    )


async def _transition_real_study(
    *,
    settings: ProlificSettings,
    study_id: str,
    action: str,
) -> dict:
    async with _build_client(settings) as client:
        response = await client.post(
            f"/studies/{study_id}/transition/",
            json={"action": action},
        )
        _raise_for_status(response)
        return response.json()


async def create_study(
    *,
    settings: ProlificSettings,
    name: str,
    description: str,
    external_study_url: str,
    estimated_completion_time: int,
    reward: int,
    total_available_places: int,
    completion_code: str,
    device_compatibility: list[str] | None = None,
) -> dict[str, str]:
    if not settings.enabled:
        raise RuntimeError("create_study called while Prolific is disabled")

    payload: dict = {
        "name": name,
        "description": description,
        "external_study_url": external_study_url,
        "estimated_completion_time": estimated_completion_time,
        "reward": reward,
        "total_available_places": total_available_places,
        "prolific_id_option": "url_parameters",
        "device_compatibility": device_compatibility or ["desktop"],
        "completion_codes": [
            {
                "code": completion_code,
                "code_type": "COMPLETED",
                "actions": [{"action": "AUTOMATICALLY_APPROVE"}],
            }
        ],
    }
    if settings.project_id:
        payload["project"] = settings.project_id

    async with _build_client(settings) as client:
        response = await client.post("/studies/", json=payload)
        _raise_for_status(response)
        return response.json()


async def publish_study(
    *,
    settings: ProlificSettings,
    study_id: str,
) -> dict[str, str]:
    if not settings.enabled:
        raise RuntimeError("publish_study called while Prolific is disabled")

    return await _transition_real_study(
        settings=settings,
        study_id=study_id,
        action="PUBLISH",
    )


async def stop_study(
    *,
    settings: ProlificSettings,
    study_id: str,
) -> dict[str, str]:
    if not settings.enabled:
        raise RuntimeError("stop_study called while Prolific is disabled")

    return await _transition_real_study(
        settings=settings,
        study_id=study_id,
        action="STOP",
    )


async def delete_study(
    *,
    settings: ProlificSettings,
    study_id: str,
) -> None:
    if not settings.enabled:
        raise RuntimeError("delete_study called while Prolific is disabled")

    async with _build_client(settings) as client:
        response = await client.delete(f"/studies/{study_id}/")
        if response.status_code == 404:
            logger.warning(
                "Prolific study already deleted (404)",
                extra={"attributes": {"study_id": study_id}},
            )
            return
        _raise_for_status(response)


async def get_study(
    *,
    settings: ProlificSettings,
    study_id: str,
) -> dict[str, str]:
    if not settings.enabled:
        raise RuntimeError("get_study called while Prolific is disabled")

    async with _build_client(settings) as client:
        response = await client.get(f"/studies/{study_id}/")
        _raise_for_status(response)
        return response.json()


async def get_project(
    *,
    settings: ProlificSettings,
    project_id: str,
) -> dict:
    if not settings.enabled:
        raise RuntimeError("get_project called while Prolific is disabled")

    async with _build_client(settings) as client:
        response = await client.get(f"/projects/{project_id}/")
        _raise_for_status(response)
        return response.json()


async def get_workspace_balance(
    *,
    settings: ProlificSettings,
    workspace_id: str,
) -> dict:
    if not settings.enabled:
        raise RuntimeError("get_workspace_balance called while Prolific is disabled")

    async with _build_client(settings) as client:
        response = await client.get(f"/workspaces/{workspace_id}/balance/")
        _raise_for_status(response)
        return response.json()


async def update_study(
    *,
    settings: ProlificSettings,
    study_id: str,
    fields: dict,
) -> dict:
    if not settings.enabled:
        raise RuntimeError("update_study called while Prolific is disabled")

    async with _build_client(settings) as client:
        response = await client.patch(f"/studies/{study_id}/", json=fields)
        _raise_for_status(response)
        return response.json()


# Workspace currency lookup is cached for the process lifetime once resolved
# successfully. The project's workspace and that workspace's currency are
# effectively immutable; changing PROLIFIC__PROJECT_ID requires a deploy/
# restart anyway, so a longer-lived cache than per-request is fine.
_cached_currency: tuple[str | None, str | None] | None = None
_currency_lock = asyncio.Lock()


def _reset_currency_cache() -> None:
    """Clear the cached workspace currency. Used by tests to isolate state."""
    global _cached_currency
    _cached_currency = None


async def _fetch_workspace_currency(
    settings: ProlificSettings,
) -> tuple[str | None, str | None]:
    if not settings.enabled or not settings.project_id:
        return (None, None)

    try:
        project = await get_project(settings=settings, project_id=settings.project_id)
        workspace_id = project.get("workspace")
        if not isinstance(workspace_id, str) or not workspace_id:
            logger.warning(
                "Prolific project response missing 'workspace'; currency lookup skipped",
                extra={"attributes": {"project_id": settings.project_id}},
            )
            return (None, None)

        balance = await get_workspace_balance(settings=settings, workspace_id=workspace_id)
        code = balance.get("currency_code")
        if not isinstance(code, str) or not code:
            return (None, None)
        return (code, CURRENCY_SYMBOLS.get(code, code))
    except Exception:
        logger.warning(
            "Failed to fetch Prolific workspace currency",
            exc_info=True,
            extra={"attributes": {"project_id": settings.project_id}},
        )
        return (None, None)


async def get_cached_workspace_currency(
    settings: ProlificSettings,
) -> tuple[str | None, str | None]:
    """Resolve and cache (currency_code, currency_symbol) for the configured project.

    Looks up the project's workspace via Prolific, then reads the workspace's
    currency. Returns (None, None) when the integration is disabled,
    PROLIFIC__PROJECT_ID is unset, or any Prolific call fails. Successful
    results are cached for the process lifetime; failures are not cached so
    transient outages self-heal on the next call.
    """
    global _cached_currency
    if _cached_currency is not None:
        return _cached_currency
    async with _currency_lock:
        if _cached_currency is not None:
            return _cached_currency
        result = await _fetch_workspace_currency(settings)
        if result != (None, None):
            _cached_currency = result
        return result
