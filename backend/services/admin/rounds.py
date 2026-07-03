"""Pilot study and experiment round management."""

from __future__ import annotations

import json
import logging
import math
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import (
    ROUND_TERMINAL_STATUSES,
    Experiment,
    ExperimentRound,
    ExperimentStatus,
    ProlificStudyStatus,
    Question,
)
from schemas import (
    ExperimentRoundCreate,
    ExperimentRoundResponse,
    ExperimentRoundUpdate,
    PilotStudyCreate,
    RecommendationResponse,
)

from .prolific import (
    ProlificAPIError,
    build_completion_url,
    build_exclusion_filters,
    build_external_study_url,
    build_screener_filters,
    build_study_url,
    create_study,
    delete_study,
    generate_completion_code,
    get_study,
    publish_study,
    stop_study,
    update_study,
)
from services.participant_groups import ensure_participant_group_and_commit
from services.prolific_markdown import to_prolific_html
from services.queries import parent_question_ids_subquery

from .queries import fetch_experiment_or_404, fetch_ratings_for_experiment
from .status import validate_new_exclusion_targets

logger = logging.getLogger(__name__)

_PROLIFIC_BODY_TRUNCATE = 500


def _prolific_error_detail(generic: str, exc: ProlificAPIError) -> str:
    body = (exc.body or "").strip()
    if not body:
        return generic
    parsed = _extract_prolific_message(body)
    if parsed:
        return f"{generic} Prolific said: {parsed}"
    if len(body) > _PROLIFIC_BODY_TRUNCATE:
        body = body[:_PROLIFIC_BODY_TRUNCATE] + "…"
    return f"{generic} Prolific said: {body}"


def _extract_prolific_message(body: str) -> str | None:
    """Pull the most useful human-readable string from a Prolific error body.

    Prolific's documented shape is ``{"error": {"detail": ..., "title": ...}}``,
    but we also accept a top-level ``detail`` and fall back to ``None`` so the
    caller can surface the raw body if the shape ever changes.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("detail", "title"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    value = payload.get("detail")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


SESSION_DURATION_SECONDS = 3600  # 1 hour per Prolific place
ROUND_BUFFER_FACTOR = 0.8
ROUND_SYNC_STATUSES = {
    ProlificStudyStatus.UNPUBLISHED,
    ProlificStudyStatus.PUBLISHING,
    ProlificStudyStatus.ACTIVE,
    ProlificStudyStatus.SCHEDULED,
    ProlificStudyStatus.PAUSED,
}


def _build_round_response(round_: ExperimentRound) -> ExperimentRoundResponse:
    return ExperimentRoundResponse(
        id=round_.id,
        round_number=round_.round_number,
        prolific_study_id=round_.prolific_study_id,
        prolific_study_status=round_.prolific_study_status,
        places_requested=round_.places_requested,
        description=round_.description,
        estimated_completion_time=round_.estimated_completion_time,
        reward=round_.reward,
        device_compatibility=_parse_device_compatibility(round_.device_compatibility),
        study_label=round_.study_label,
        screeners=_parse_screeners(round_.screeners),
        excluded_experiment_ids=_parse_excluded_experiment_ids(round_.excluded_experiment_ids),
        created_at=round_.created_at,
        prolific_study_url=build_study_url(study_id=round_.prolific_study_id),
    )


def _ensure_completion_code(experiment: Experiment) -> str:
    if experiment.prolific_completion_url:
        parsed = urlparse(experiment.prolific_completion_url)
        completion_code = parse_qs(parsed.query).get("cc", [None])[0]
        if completion_code:
            return completion_code

    completion_code = generate_completion_code()
    experiment.prolific_completion_url = build_completion_url(completion_code)
    return completion_code


def _parse_device_compatibility(device_compatibility: str) -> list[str]:
    return json.loads(device_compatibility)


def _parse_screeners(screeners: str | None) -> list[str]:
    if not screeners:
        return []
    return json.loads(screeners)


def _parse_excluded_experiment_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    return json.loads(raw)


async def _build_round_blocklist_group_ids(
    experiment: Experiment,
    excluded_experiment_ids: list[int],
    db: AsyncSession,
    *,
    strict: bool,
) -> list[str]:
    """Full participant-group blocklist for a round of `experiment`.

    Combines the experiment's own group with the resolved groups of any
    explicitly excluded prior experiments. Blocking the own group keeps raters
    from one round of the experiment out of every other round of the same
    experiment — Prolific groups are dynamic, so a group that's still empty at
    launch time (e.g. the pilot) starts filtering as soon as any rater joins.
    """
    own_group_id = await ensure_participant_group_and_commit(experiment, db)
    others = await _resolve_exclusion_group_ids(excluded_experiment_ids, db, strict=strict)
    # Dedupe while preserving order: if the admin somehow ends up with the
    # current experiment in its own exclusion list, we still send Prolific a
    # single blocklist entry per group.
    result: list[str] = []
    if own_group_id:
        result.append(own_group_id)
    result.extend(others)
    return list(dict.fromkeys(result))


async def _resolve_exclusion_group_ids(
    excluded_experiment_ids: list[int],
    db: AsyncSession,
    *,
    strict: bool,
) -> list[str]:
    """For each experiment ID in the exclusion list, return its participant
    group ID, creating the group lazily if the experiment doesn't have one yet.

    Lazy creation is important: even if experiment A has never had a rater, we
    still want to attach A's (empty) group to A*'s blocklist so future entrants
    to A are automatically excluded from A*.

    `strict=True` means an ID with no matching experiment is a hard error —
    used on paths where the admin just picked the IDs and a silent drop would
    ship a broken blocklist. `strict=False` logs and skips — used when the IDs
    are inherited from an earlier round and one may have been deleted since,
    where blocking an unrelated edit would be more annoying than useful.
    """
    if not excluded_experiment_ids:
        return []

    experiments_by_id = {
        exp.id: exp
        for exp in (
            await db.execute(select(Experiment).where(Experiment.id.in_(excluded_experiment_ids)))
        ).scalars()
    }

    group_ids: list[str] = []
    for exp_id in excluded_experiment_ids:
        experiment = experiments_by_id.get(exp_id)
        if experiment is None:
            if strict:
                raise HTTPException(
                    status_code=400,
                    detail=f"Excluded experiment {exp_id} does not exist.",
                )
            logger.warning(
                "Excluded experiment not found; skipping",
                extra={"attributes": {"excluded_experiment_id": exp_id}},
            )
            continue
        group_id = await ensure_participant_group_and_commit(experiment, db)
        if group_id:
            group_ids.append(group_id)
    return group_ids


def _is_round_closed(round_: ExperimentRound) -> bool:
    return round_.prolific_study_status in ROUND_TERMINAL_STATUSES


def _build_round_study_name(experiment_name: str, round_number: int) -> str:
    suffix = "Pilot" if round_number == 0 else f"Round {round_number}"
    return f"{experiment_name} - {suffix}"


def _build_round_internal_name(
    experiment_internal_name: str | None, round_number: int
) -> str | None:
    """Per-round Prolific internal_name: includes the round suffix so the
    researcher can disambiguate rounds of the same experiment in Prolific's
    study list. Returns None when no internal name was set on the experiment
    so we don't send an empty field.
    """
    if not experiment_internal_name or not experiment_internal_name.strip():
        return None
    suffix = "Pilot" if round_number == 0 else f"Round {round_number}"
    return f"{experiment_internal_name.strip()} - {suffix}"


async def _refresh_round_statuses(rounds: list[ExperimentRound], db: AsyncSession) -> None:
    settings = get_settings()
    if not settings.prolific.enabled:
        return

    changed = False
    for round_ in rounds:
        if round_.prolific_study_status not in ROUND_SYNC_STATUSES:
            continue
        try:
            prolific_study = await get_study(
                settings=settings.prolific,
                study_id=round_.prolific_study_id,
            )
            status = prolific_study.get("status")
            if not status:
                continue
            updated_status = ProlificStudyStatus(status)
        except Exception:
            logger.warning(
                "Failed to refresh Prolific status for round; using cached status",
                exc_info=True,
                extra={
                    "attributes": {
                        "round_id": round_.id,
                        "study_id": round_.prolific_study_id,
                    }
                },
            )
            continue

        if round_.prolific_study_status != updated_status:
            round_.prolific_study_status = updated_status
            changed = True

    if changed:
        await db.commit()


async def _cleanup_orphaned_study(study_id: str) -> None:
    settings = get_settings()
    if not settings.prolific.enabled:
        return

    try:
        await delete_study(
            settings=settings.prolific,
            study_id=study_id,
        )
    except Exception:
        logger.error(
            "Failed to clean up orphaned Prolific study after local DB failure",
            exc_info=True,
            extra={"attributes": {"study_id": study_id}},
        )


async def _commit_round_creation(
    db: AsyncSession,
    round_: ExperimentRound,
    *,
    conflict_detail: str,
    generic_detail: str,
) -> None:
    db.add(round_)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        await _cleanup_orphaned_study(round_.prolific_study_id)
        raise HTTPException(status_code=409, detail=conflict_detail) from exc
    except Exception as exc:
        await db.rollback()
        await _cleanup_orphaned_study(round_.prolific_study_id)
        logger.error(
            "Failed to save local round record after creating Prolific study",
            exc_info=True,
            extra={"attributes": {"study_id": round_.prolific_study_id}},
        )
        raise HTTPException(status_code=500, detail=generic_detail) from exc


async def _fetch_round_or_404(
    experiment_id: int,
    round_id: int,
    db: AsyncSession,
) -> ExperimentRound:
    round_ = (
        await db.execute(
            select(ExperimentRound).where(
                ExperimentRound.id == round_id,
                ExperimentRound.experiment_id == experiment_id,
            )
        )
    ).scalar_one_or_none()
    if round_ is None:
        raise HTTPException(status_code=404, detail="Experiment round not found")
    return round_


async def _list_round_models(
    experiment_id: int,
    db: AsyncSession,
) -> list[ExperimentRound]:
    return (
        (
            await db.execute(
                select(ExperimentRound)
                .where(ExperimentRound.experiment_id == experiment_id)
                .order_by(ExperimentRound.round_number)
            )
        )
        .scalars()
        .all()
    )


async def _create_prolific_study_for_round(
    experiment: Experiment,
    *,
    round_number: int,
    description: str,
    estimated_completion_time: int,
    reward: int,
    places: int,
    device_compatibility: list[str],
    study_label: str | None,
    screeners: list[str],
    excluded_participant_group_ids: list[str],
) -> dict[str, str]:
    settings = get_settings()
    completion_code = _ensure_completion_code(experiment)
    external_study_url = build_external_study_url(
        site_url=settings.app.site_url,
        experiment_id=experiment.id,
    )

    return await create_study(
        settings=settings.prolific,
        name=_build_round_study_name(experiment.name, round_number),
        internal_name=_build_round_internal_name(experiment.internal_name, round_number),
        description=to_prolific_html(description),
        external_study_url=external_study_url,
        estimated_completion_time=estimated_completion_time,
        reward=reward,
        total_available_places=places,
        completion_code=completion_code,
        device_compatibility=device_compatibility,
        study_label=study_label,
        screeners=screeners,
        excluded_participant_group_ids=excluded_participant_group_ids,
    )


async def calculate_recommendation(
    experiment_id: int,
    db: AsyncSession,
    *,
    include_preview: bool = False,
) -> RecommendationResponse:
    experiment = await fetch_experiment_or_404(experiment_id, db)
    ratings = await fetch_ratings_for_experiment(
        experiment_id,
        db,
        include_preview=include_preview,
    )

    if not ratings:
        return RecommendationResponse(
            avg_time_per_question_seconds=0.0,
            remaining_rating_actions=0,
            total_hours_remaining=0.0,
            recommended_places=0,
            is_complete=False,
        )

    times = [
        (rating.time_submitted - rating.time_started).total_seconds() for rating, _, _ in ratings
    ]
    avg_time = sum(times) / len(times)

    rating_counts: dict[int, int] = {}
    for rating, question, _ in ratings:
        rating_counts[question.id] = rating_counts.get(question.id, 0) + 1

    all_question_ids = (
        (
            await db.execute(
                select(Question.id)
                .where(Question.experiment_id == experiment_id)
                .where(Question.id.notin_(parent_question_ids_subquery()))
            )
        )
        .scalars()
        .all()
    )

    target = experiment.num_ratings_per_question
    remaining_actions = sum(max(0, target - rating_counts.get(qid, 0)) for qid in all_question_ids)

    is_complete = remaining_actions == 0
    total_hours = (remaining_actions * avg_time) / SESSION_DURATION_SECONDS
    recommended_places = math.ceil(total_hours * ROUND_BUFFER_FACTOR) if not is_complete else 0

    return RecommendationResponse(
        avg_time_per_question_seconds=round(avg_time, 2),
        remaining_rating_actions=remaining_actions,
        total_hours_remaining=round(total_hours, 2),
        recommended_places=recommended_places,
        is_complete=is_complete,
    )


async def run_pilot_study(
    experiment_id: int,
    payload: PilotStudyCreate,
    db: AsyncSession,
) -> ExperimentRoundResponse:
    settings = get_settings()
    if not settings.prolific.enabled:
        raise HTTPException(status_code=400, detail="Prolific integration is not enabled")

    experiment = await fetch_experiment_or_404(experiment_id, db)
    existing_rounds = await _list_round_models(experiment_id, db)
    if existing_rounds:
        raise HTTPException(
            status_code=400,
            detail="A pilot study has already been run for this experiment",
        )

    excluded_experiment_ids = list(payload.excluded_experiment_ids)
    # Pilot creation is the first write for this experiment's exclusion list —
    # every listed target is "new", so all must be FINISHED. Grandfathering
    # only kicks in on subsequent edits when IDs were already present.
    await validate_new_exclusion_targets(
        excluded_experiment_ids,
        previously_allowed_ids=[],
        db=db,
    )
    blocklist_group_ids = await _build_round_blocklist_group_ids(
        experiment, excluded_experiment_ids, db, strict=True
    )

    try:
        result = await _create_prolific_study_for_round(
            experiment,
            round_number=0,
            description=payload.description,
            estimated_completion_time=payload.estimated_completion_time,
            reward=payload.reward,
            places=payload.pilot_places,
            device_compatibility=payload.device_compatibility,
            study_label=payload.study_label,
            screeners=list(payload.screeners),
            excluded_participant_group_ids=blocklist_group_ids,
        )
    except HTTPException:
        raise
    except ProlificAPIError as exc:
        logger.error(
            "Failed to create pilot Prolific study",
            extra={
                "attributes": {
                    "experiment_id": experiment_id,
                    "prolific_status": exc.status_code,
                    "prolific_body": exc.body,
                }
            },
        )
        raise HTTPException(
            status_code=502,
            detail=_prolific_error_detail("Failed to create study on Prolific.", exc),
        )
    except Exception:
        logger.error(
            "Failed to create pilot Prolific study",
            exc_info=True,
            extra={"attributes": {"experiment_id": experiment_id}},
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to create study on Prolific. Please check your API token and try again.",
        )

    round_ = ExperimentRound(
        experiment_id=experiment_id,
        round_number=0,
        prolific_study_id=result["id"],
        prolific_study_status=ProlificStudyStatus(result.get("status", "UNPUBLISHED")),
        description=payload.description,
        estimated_completion_time=payload.estimated_completion_time,
        reward=payload.reward,
        device_compatibility=json.dumps(payload.device_compatibility),
        study_label=payload.study_label,
        screeners=json.dumps(list(payload.screeners)),
        excluded_experiment_ids=json.dumps(excluded_experiment_ids),
        places_requested=payload.pilot_places,
    )
    await _commit_round_creation(
        db,
        round_,
        conflict_detail="A pilot study has already been run for this experiment",
        generic_detail="Failed to save pilot study after creating it on Prolific. Please try again.",
    )
    await db.refresh(round_)

    logger.info(
        "Prolific pilot study created",
        extra={
            "attributes": {
                "experiment_id": experiment_id,
                "round_id": round_.id,
                "study_id": round_.prolific_study_id,
            }
        },
    )
    return _build_round_response(round_)


async def run_experiment_round(
    experiment_id: int,
    payload: ExperimentRoundCreate,
    db: AsyncSession,
) -> ExperimentRoundResponse:
    settings = get_settings()
    if not settings.prolific.enabled:
        raise HTTPException(status_code=400, detail="Prolific integration is not enabled")

    experiment = await fetch_experiment_or_404(experiment_id, db)
    if experiment.status == ExperimentStatus.FINISHED:
        raise HTTPException(
            status_code=400,
            detail="Cannot launch new rounds: experiment is finished.",
        )
    rounds = await _list_round_models(experiment_id, db)
    if not rounds:
        raise HTTPException(
            status_code=400,
            detail="Run a pilot study first before launching a main round",
        )

    pilot_round = rounds[0]
    latest_round = rounds[-1]
    if not _is_round_closed(latest_round):
        raise HTTPException(
            status_code=400,
            detail="Close the previous round before launching a new round",
        )

    next_round_number = latest_round.round_number + 1
    device_compatibility = _parse_device_compatibility(pilot_round.device_compatibility)
    screeners = _parse_screeners(pilot_round.screeners)
    excluded_experiment_ids = _parse_excluded_experiment_ids(pilot_round.excluded_experiment_ids)
    blocklist_group_ids = await _build_round_blocklist_group_ids(
        experiment, excluded_experiment_ids, db, strict=False
    )

    try:
        result = await _create_prolific_study_for_round(
            experiment,
            round_number=next_round_number,
            description=pilot_round.description,
            estimated_completion_time=pilot_round.estimated_completion_time,
            reward=pilot_round.reward,
            places=payload.places,
            device_compatibility=device_compatibility,
            study_label=pilot_round.study_label,
            screeners=screeners,
            excluded_participant_group_ids=blocklist_group_ids,
        )
    except HTTPException:
        raise
    except ProlificAPIError as exc:
        logger.error(
            "Failed to create experiment round Prolific study",
            extra={
                "attributes": {
                    "experiment_id": experiment_id,
                    "round_number": next_round_number,
                    "prolific_status": exc.status_code,
                    "prolific_body": exc.body,
                }
            },
        )
        raise HTTPException(
            status_code=502,
            detail=_prolific_error_detail("Failed to create round on Prolific.", exc),
        )
    except Exception:
        logger.error(
            "Failed to create experiment round Prolific study",
            exc_info=True,
            extra={
                "attributes": {
                    "experiment_id": experiment_id,
                    "round_number": next_round_number,
                }
            },
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to create study on Prolific. Please check your API token and try again.",
        )

    round_ = ExperimentRound(
        experiment_id=experiment_id,
        round_number=next_round_number,
        prolific_study_id=result["id"],
        prolific_study_status=ProlificStudyStatus(result.get("status", "UNPUBLISHED")),
        description=pilot_round.description,
        estimated_completion_time=pilot_round.estimated_completion_time,
        reward=pilot_round.reward,
        device_compatibility=pilot_round.device_compatibility,
        study_label=pilot_round.study_label,
        screeners=pilot_round.screeners,
        excluded_experiment_ids=pilot_round.excluded_experiment_ids,
        places_requested=payload.places,
    )
    # First main round transitions the experiment into LAUNCH and freezes its
    # config. Set here so the status flip and the round insert land in one
    # commit — a failed insert rolls the status back too. Idempotent: LAUNCH
    # stays LAUNCH on subsequent rounds.
    if experiment.status == ExperimentStatus.DRAFT:
        experiment.status = ExperimentStatus.LAUNCH
    await _commit_round_creation(
        db,
        round_,
        conflict_detail="A round with this number already exists for this experiment",
        generic_detail="Failed to save round after creating it on Prolific. Please try again.",
    )
    await db.refresh(round_)

    logger.info(
        "Prolific experiment round created",
        extra={
            "attributes": {
                "experiment_id": experiment_id,
                "round_number": next_round_number,
                "round_id": round_.id,
                "study_id": round_.prolific_study_id,
            }
        },
    )
    return _build_round_response(round_)


async def publish_experiment_round(
    experiment_id: int,
    round_id: int,
    db: AsyncSession,
) -> dict[str, str]:
    settings = get_settings()
    if not settings.prolific.enabled:
        raise HTTPException(status_code=400, detail="Prolific integration is not enabled")

    await fetch_experiment_or_404(experiment_id, db)
    round_ = await _fetch_round_or_404(experiment_id, round_id, db)
    if round_.prolific_study_status != ProlificStudyStatus.UNPUBLISHED:
        raise HTTPException(
            status_code=400,
            detail="Only unpublished rounds can be published",
        )

    try:
        result = await publish_study(
            settings=settings.prolific,
            study_id=round_.prolific_study_id,
        )
    except ProlificAPIError as exc:
        logger.error(
            "Failed to publish Prolific study",
            extra={
                "attributes": {
                    "experiment_id": experiment_id,
                    "round_id": round_id,
                    "study_id": round_.prolific_study_id,
                    "prolific_status": exc.status_code,
                    "prolific_body": exc.body,
                }
            },
        )
        raise HTTPException(
            status_code=502,
            detail=_prolific_error_detail("Failed to publish study on Prolific.", exc),
        )
    except Exception:
        logger.error(
            "Failed to publish Prolific study",
            exc_info=True,
            extra={
                "attributes": {
                    "experiment_id": experiment_id,
                    "round_id": round_id,
                    "study_id": round_.prolific_study_id,
                }
            },
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to publish study on Prolific. Please try again.",
        )

    round_.prolific_study_status = ProlificStudyStatus(
        result.get("status", ProlificStudyStatus.ACTIVE.value)
    )
    await db.commit()

    logger.info(
        "Prolific study published",
        extra={
            "attributes": {
                "experiment_id": experiment_id,
                "round_id": round_id,
                "study_id": round_.prolific_study_id,
            }
        },
    )
    return {"message": "Study published on Prolific", "status": round_.prolific_study_status}


_PROLIFIC_FIELD_MAP = {
    "estimated_completion_time": "estimated_completion_time",
    "reward": "reward",
    "places": "total_available_places",
}


async def update_experiment_round(
    experiment_id: int,
    round_id: int,
    payload: ExperimentRoundUpdate,
    db: AsyncSession,
) -> ExperimentRoundResponse:
    settings = get_settings()
    if not settings.prolific.enabled:
        raise HTTPException(status_code=400, detail="Prolific integration is not enabled")

    if not payload.has_any():
        raise HTTPException(
            status_code=400,
            detail="Provide at least one field to update.",
        )

    experiment = await fetch_experiment_or_404(experiment_id, db)
    round_ = await _fetch_round_or_404(experiment_id, round_id, db)
    if round_.prolific_study_status != ProlificStudyStatus.UNPUBLISHED:
        raise HTTPException(
            status_code=400,
            detail="Only unpublished rounds can be edited",
        )

    prolific_fields: dict = {}
    for src, dst in _PROLIFIC_FIELD_MAP.items():
        value = getattr(payload, src)
        if value is not None:
            prolific_fields[dst] = value
    if payload.device_compatibility is not None:
        prolific_fields["device_compatibility"] = payload.device_compatibility
    if payload.description is not None:
        # Convert markdown to Prolific's HTML subset on the wire, but keep
        # the raw markdown in our DB so editors see what they typed.
        prolific_fields["description"] = to_prolific_html(payload.description)
    if payload.study_label is not None:
        prolific_fields["study_labels"] = [payload.study_label]
    # `filters` on Prolific is a full replacement, so we always rebuild the
    # combined screener + exclusion filter list when either side changes.
    if payload.screeners is not None or payload.excluded_experiment_ids is not None:
        screeners = (
            list(payload.screeners)
            if payload.screeners is not None
            else _parse_screeners(round_.screeners)
        )
        previously_allowed_ids = _parse_excluded_experiment_ids(round_.excluded_experiment_ids)
        excluded_ids = (
            list(payload.excluded_experiment_ids)
            if payload.excluded_experiment_ids is not None
            else previously_allowed_ids
        )
        # New targets on this write must be FINISHED. IDs already present on
        # this round are grandfathered: they were legal when set and the target
        # experiment's later state change shouldn't retroactively invalidate
        # them.
        if payload.excluded_experiment_ids is not None:
            await validate_new_exclusion_targets(
                excluded_ids,
                previously_allowed_ids=previously_allowed_ids,
                db=db,
            )
        # Strict only when the admin is explicitly setting the list — an
        # inherited-and-stale ID from an earlier round shouldn't block an
        # unrelated field edit (reward, screeners, etc.).
        blocklist_group_ids = await _build_round_blocklist_group_ids(
            experiment,
            excluded_ids,
            db,
            strict=payload.excluded_experiment_ids is not None,
        )
        prolific_fields["filters"] = build_screener_filters(screeners) + build_exclusion_filters(
            blocklist_group_ids
        )

    try:
        await update_study(
            settings=settings.prolific,
            study_id=round_.prolific_study_id,
            fields=prolific_fields,
        )
    except Exception:
        logger.error(
            "Failed to update Prolific study",
            exc_info=True,
            extra={
                "attributes": {
                    "experiment_id": experiment_id,
                    "round_id": round_id,
                    "study_id": round_.prolific_study_id,
                    "fields_updated": sorted(prolific_fields.keys()),
                }
            },
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to update study on Prolific. Please try again.",
        )

    if payload.description is not None:
        round_.description = payload.description
    if payload.estimated_completion_time is not None:
        round_.estimated_completion_time = payload.estimated_completion_time
    if payload.reward is not None:
        round_.reward = payload.reward
    if payload.places is not None:
        round_.places_requested = payload.places
    if payload.device_compatibility is not None:
        round_.device_compatibility = json.dumps(payload.device_compatibility)
    if payload.study_label is not None:
        round_.study_label = payload.study_label
    if payload.screeners is not None:
        round_.screeners = json.dumps(list(payload.screeners))
    if payload.excluded_experiment_ids is not None:
        round_.excluded_experiment_ids = json.dumps(list(payload.excluded_experiment_ids))
    await db.commit()
    await db.refresh(round_)

    logger.info(
        "Prolific study updated",
        extra={
            "attributes": {
                "experiment_id": experiment_id,
                "round_id": round_id,
                "study_id": round_.prolific_study_id,
                "fields_updated": sorted(prolific_fields.keys()),
            }
        },
    )
    return _build_round_response(round_)


async def close_experiment_round(
    experiment_id: int,
    round_id: int,
    db: AsyncSession,
) -> dict[str, str]:
    settings = get_settings()
    if not settings.prolific.enabled:
        raise HTTPException(status_code=400, detail="Prolific integration is not enabled")

    await fetch_experiment_or_404(experiment_id, db)
    round_ = await _fetch_round_or_404(experiment_id, round_id, db)
    if _is_round_closed(round_):
        raise HTTPException(status_code=400, detail="This round is already closed")

    try:
        result = await stop_study(
            settings=settings.prolific,
            study_id=round_.prolific_study_id,
        )
    except ProlificAPIError as exc:
        logger.error(
            "Failed to close Prolific study",
            extra={
                "attributes": {
                    "experiment_id": experiment_id,
                    "round_id": round_id,
                    "study_id": round_.prolific_study_id,
                    "prolific_status": exc.status_code,
                    "prolific_body": exc.body,
                }
            },
        )
        raise HTTPException(
            status_code=502,
            detail=_prolific_error_detail("Failed to close study on Prolific.", exc),
        )
    except Exception:
        logger.error(
            "Failed to close Prolific study",
            exc_info=True,
            extra={
                "attributes": {
                    "experiment_id": experiment_id,
                    "round_id": round_id,
                    "study_id": round_.prolific_study_id,
                }
            },
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to close study on Prolific. Please try again.",
        )

    status = result.get("status")
    if not status:
        raise HTTPException(
            status_code=502,
            detail="Unexpected response from Prolific when closing the study.",
        )

    round_.prolific_study_status = ProlificStudyStatus(status)
    await db.commit()

    logger.info(
        "Prolific round closed",
        extra={
            "attributes": {
                "experiment_id": experiment_id,
                "round_id": round_id,
                "study_id": round_.prolific_study_id,
            }
        },
    )
    return {"message": "Round closed on Prolific", "status": round_.prolific_study_status}


async def list_experiment_rounds(
    experiment_id: int,
    db: AsyncSession,
) -> list[ExperimentRoundResponse]:
    await fetch_experiment_or_404(experiment_id, db)
    rounds = await _list_round_models(experiment_id, db)
    await _refresh_round_statuses(rounds, db)
    return [_build_round_response(round_) for round_ in rounds]
