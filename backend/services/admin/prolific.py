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
from typing import NamedTuple

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

# Screener key -> Prolific `filters` entry. Names mirror Prolific's UI labels
# so admins recognise them in the experiment form.
# Filter catalogue: GET https://api.prolific.com/api/v1/filters/
# Docs: https://docs.prolific.com/docs/api-docs/public/#tag/Filters
SCREENER_FILTERS: dict[str, dict] = {
    # Qualified AI taskers pool (select filter; "0" = "Qualified AI taskers").
    "ai_taskers": {"filter_id": "ai-taskers", "selected_values": ["0"]},
    # Fact Checkers group (select filter; "0" = "Fact Checkers"). Expert network
    # of participants with fact-checking experience — no exam score threshold.
    "fact_checkers": {"filter_id": "fact-checkers", "selected_values": ["0"]},
    # Overall approval rate on past Prolific submissions (integer 0–100).
    # This is the "80%+ approval rate" filter used in the fall 2024 paper.
    "approval_rate": {
        "filter_id": "approval_rate",
        "selected_range": {"lower": 80, "upper": 100},
    },
}


def build_screener_filters(screeners: list[str] | None) -> list[dict]:
    """Translate screener keys (e.g. 'ai_taskers') into Prolific filter payloads.

    Used by both `create_study` and the round-update path. An empty/None input
    yields an empty list, which callers treat as 'no filters'.
    """
    if not screeners:
        return []
    return [SCREENER_FILTERS[name] for name in screeners if name in SCREENER_FILTERS]


def build_exclusion_filters(participant_group_ids: list[str] | None) -> list[dict]:
    """Wrap participant group IDs in a `participant_group_blocklist` filter entry.

    Returns a single-element list (for composition with `build_screener_filters`)
    or empty when there are no groups to block. Group IDs come from prior
    experiments the round should exclude — Prolific will hide the study from
    anyone in any of the listed groups.
    """
    ids = [gid for gid in (participant_group_ids or []) if gid]
    if not ids:
        return []
    return [{"filter_id": "participant_group_blocklist", "selected_values": ids}]


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
    internal_name: str | None = None,
    study_label: str | None = None,
    screeners: list[str] | None = None,
    excluded_participant_group_ids: list[str] | None = None,
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
    if internal_name:
        payload["internal_name"] = internal_name
    if study_label:
        payload["study_labels"] = [study_label]
    filters = build_screener_filters(screeners) + build_exclusion_filters(
        excluded_participant_group_ids
    )
    if filters:
        payload["filters"] = filters
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


# Prolific submission statuses, split by what they mean for a round's progress.
# Observed across this workspace's studies: ACTIVE, APPROVED, RETURNED,
# TIMED-OUT. The rest are documented statuses we classify defensively so a study
# under manual review or with rejections still tallies sensibly.
#
# A place is consumed by exactly the submitted and in-progress statuses:
# `places_taken` on the study equals their sum (verified against a live study
# with 35 APPROVED + 1 ACTIVE and places_taken 36). Returned, timed-out,
# rejected and screened-out submissions release the place for someone else, so
# they belong to neither bucket.
SUBMISSION_SUBMITTED_STATUSES = frozenset({"AWAITING REVIEW", "APPROVED", "PARTIALLY APPROVED"})
SUBMISSION_IN_PROGRESS_STATUSES = frozenset({"ACTIVE", "RESERVED"})


class SubmissionCounts(NamedTuple):
    """How many of a study's raters have submitted vs are still working."""

    completed: int
    in_progress: int


def summarize_submissions(results: list[dict]) -> SubmissionCounts:
    completed = 0
    in_progress = 0
    unknown: set[str] = set()
    for submission in results:
        status = submission.get("status")
        if status in SUBMISSION_SUBMITTED_STATUSES:
            completed += 1
        elif status in SUBMISSION_IN_PROGRESS_STATUSES:
            in_progress += 1
        elif isinstance(status, str) and status:
            # RETURNED / TIMED-OUT / REJECTED / SCREENED OUT release the place,
            # so they are counted in neither bucket. Anything genuinely new gets
            # logged once rather than silently landing in a bucket.
            unknown.add(status)
    if unknown - {"RETURNED", "TIMED-OUT", "REJECTED", "SCREENED OUT"}:
        logger.info(
            "Unclassified Prolific submission statuses; counted as neither "
            "completed nor in progress",
            extra={"attributes": {"statuses": sorted(unknown)}},
        )
    return SubmissionCounts(completed=completed, in_progress=in_progress)


async def get_study_submission_counts(
    *,
    settings: ProlificSettings,
    study_id: str,
) -> SubmissionCounts:
    """Tally a study's submissions by status.

    Prolific has no count-only endpoint and ignores `limit`, so this returns the
    full submission list in one request; `meta.count` is checked against what
    arrived so a future paginated response is logged rather than under-reported.

    Raises ValueError when a 200 carries no submission list. Returning zeros
    there would look like a real result and overwrite a round's cached counts,
    so an unusable body has to fail the same way a non-2xx does. A study with no
    submissions yet sends `results: []`, which is a list and correctly tallies
    to zeros.
    """
    if not settings.enabled:
        raise RuntimeError("get_study_submission_counts called while Prolific is disabled")

    async with _build_client(settings) as client:
        response = await client.get(f"/studies/{study_id}/submissions/")
        _raise_for_status(response)
        payload = response.json()

    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError(f"Prolific submissions response for {study_id} has no results list")

    total = (payload.get("meta") or {}).get("count")
    if isinstance(total, int) and total > len(results):
        logger.warning(
            "Prolific returned a partial submission page; counts may be low",
            extra={
                "attributes": {
                    "study_id": study_id,
                    "returned": len(results),
                    "count": total,
                }
            },
        )

    return summarize_submissions(results)


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


async def create_participant_group(
    *,
    settings: ProlificSettings,
    name: str,
) -> dict:
    """Create a Prolific participant group under the configured project.

    The returned dict includes an `id` we persist on the Experiment so future
    rounds can reference this group as a blocklist. Groups are dynamic — adding
    a participant later automatically updates eligibility on already-launched
    studies that reference the group.
    """
    if not settings.enabled:
        raise RuntimeError("create_participant_group called while Prolific is disabled")
    if not settings.project_id:
        raise RuntimeError("PROLIFIC__PROJECT_ID must be set to create participant groups")

    async with _build_client(settings) as client:
        response = await client.post(
            "/participant-groups/",
            json={"name": name, "project_id": settings.project_id},
        )
        _raise_for_status(response)
        return response.json()


async def add_participant_to_group(
    *,
    settings: ProlificSettings,
    group_id: str,
    prolific_id: str,
) -> None:
    """Add a single Prolific participant to a group.

    Idempotent from the caller's perspective — Prolific returns 400 for unknown
    participant IDs (error_code 140003) and treats already-present participants
    as a no-op. Errors are surfaced; callers wrap in try/except and log-and-
    continue when the add is best-effort (e.g. rater start_session).
    """
    if not settings.enabled:
        raise RuntimeError("add_participant_to_group called while Prolific is disabled")

    async with _build_client(settings) as client:
        response = await client.post(
            f"/participant-groups/{group_id}/participants/",
            json={"participant_ids": [prolific_id]},
        )
        _raise_for_status(response)


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


# ── Study pricing rates ─────────────────────────────────────────────────────
#
# A study's `total_cost` is rewards + Prolific's platform fee + VAT on that fee,
# which is the figure Prolific's own study page totals. Reproducing it before a
# study exists needs the workspace's rates, and the API only carries them on a
# study object (`fees_percentage`, `vat_percentage`, `fees_per_submission`).
# `/users/me/` also reports rates, but they are the researcher's own: a US
# researcher publishing into a UK-billed workspace reads vat_percentage 0.0
# there while their studies are charged 0.2. So we prefer an existing study's
# rates and fall back to the researcher's only when no study is available.


class ProlificPricing(NamedTuple):
    """Fee rates behind a study's `total_cost`, as fractions (0.2 = 20%).

    `fees_percentage` and `fees_per_submission` apply to the reward subtotal;
    `vat_percentage` applies to the fee, not to the rewards.
    """

    fees_percentage: float
    vat_percentage: float
    fees_per_submission: float


_cached_pricing: ProlificPricing | None = None
_pricing_lock = asyncio.Lock()


def _reset_pricing_cache() -> None:
    """Clear the cached pricing rates. Used by tests to isolate state."""
    global _cached_pricing
    _cached_pricing = None


def _pricing_from_payload(payload: dict) -> ProlificPricing | None:
    """Read the three rate fields off a study or user payload.

    Returns None when the fee rate is missing or unparseable — VAT and the
    per-submission fee are legitimately 0.0, but a missing fee percentage means
    the payload isn't a pricing source and estimating from it would understate
    the cost.
    """
    fees = payload.get("fees_percentage")
    if not isinstance(fees, (int, float)):
        return None
    vat = payload.get("vat_percentage")
    per_submission = payload.get("fees_per_submission")
    return ProlificPricing(
        fees_percentage=float(fees),
        vat_percentage=float(vat) if isinstance(vat, (int, float)) else 0.0,
        fees_per_submission=(
            float(per_submission) if isinstance(per_submission, (int, float)) else 0.0
        ),
    )


async def get_current_user(*, settings: ProlificSettings) -> dict:
    if not settings.enabled:
        raise RuntimeError("get_current_user called while Prolific is disabled")

    async with _build_client(settings) as client:
        response = await client.get("/users/me/")
        _raise_for_status(response)
        return response.json()


async def _fetch_pricing(
    settings: ProlificSettings,
    reference_study_id: str | None,
) -> tuple[ProlificPricing | None, bool]:
    """Return (pricing, came_from_a_study).

    The flag drives caching: researcher-sourced rates are a stand-in until a
    study exists, so they must not be cached (see `get_cached_pricing`).
    """
    if reference_study_id:
        try:
            study = await get_study(settings=settings, study_id=reference_study_id)
            pricing = _pricing_from_payload(study)
            if pricing is not None:
                return pricing, True
        except Exception:
            logger.warning(
                "Failed to read Prolific pricing from reference study; trying the researcher",
                exc_info=True,
                extra={"attributes": {"study_id": reference_study_id}},
            )

    try:
        return _pricing_from_payload(await get_current_user(settings=settings)), False
    except Exception:
        logger.warning("Failed to fetch Prolific pricing rates", exc_info=True)
        return None, False


async def get_cached_pricing(
    settings: ProlificSettings,
    *,
    reference_study_id: str | None = None,
) -> ProlificPricing | None:
    """Resolve and cache the workspace's fee/VAT rates.

    `reference_study_id` should be a study in the workspace we're pricing for
    (callers pass the most recent round's study). Returns None when the
    integration is disabled or every source fails.

    Only study-sourced rates are cached. A researcher-sourced fallback is
    returned uncached, so the first round to exist upgrades the rates instead of
    a pre-pilot lookup pinning the researcher's own VAT (0.0 for a US researcher
    in a UK-billed workspace) for the life of the process. Failures are not
    cached either, so a transient outage self-heals on the next call.
    """
    global _cached_pricing
    if not settings.enabled:
        return None
    if _cached_pricing is not None:
        return _cached_pricing
    async with _pricing_lock:
        if _cached_pricing is not None:
            return _cached_pricing
        result, from_study = await _fetch_pricing(settings, reference_study_id)
        if result is not None and from_study:
            _cached_pricing = result
        return result
