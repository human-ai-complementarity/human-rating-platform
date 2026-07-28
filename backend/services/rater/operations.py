from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    ASSIGNMENT_TTL_MINUTES,
    AssistanceSession,
    ExperimentRound,
    ProlificStudyStatus,
    QuestionAssignment,
    Rating,
    Rater,
)
from config import Settings, get_settings
from schemas import (
    QuestionResponse,
    RaterStartResponse,
    RatingResponse,
    RatingSubmit,
    SessionStatusResponse,
)
from services.admin.prolific import ProlificAPIError, add_participant_to_group, stop_study
from services.assistance import get_rater_instructions
from services.participant_groups import ensure_participant_group_and_commit
from services.queries import fetch_remaining_rating_actions
from .mappers import (
    build_question_response,
    build_rater_start_response,
    build_session_end_time,
)
from .session_token import issue_rater_session_token
from .queries import (
    fetch_assignment_for_question,
    fetch_eligible_questions_with_counts,
    fetch_existing_rater_for_experiment,
    fetch_existing_rating,
    fetch_experiment_or_404,
    fetch_in_progress_parent_ids,
    fetch_live_assignment_for_rater,
    fetch_parent_question_text,
    fetch_question_or_404,
    fetch_rated_question_ids,
    fetch_rater_completed_count,
    fetch_rater_or_404,
    fetch_round_description,
)
from .selectors import build_question_selection_groups, build_selected_question
from .validators import (
    validate_existing_rater_can_resume,
    validate_question_belongs_to_rater_experiment,
    validate_rating_confidence,
    validate_rater_marked_active,
    validate_rater_session_is_active,
)

logger = logging.getLogger(__name__)


def _normalize_to_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def start_session(
    *,
    settings: Settings,
    experiment_id: int,
    prolific_pid: str,
    study_id: str,
    session_id: str,
    is_preview: bool = False,
    db: AsyncSession,
) -> RaterStartResponse:
    experiment = await fetch_experiment_or_404(experiment_id, db)
    round_description = await fetch_round_description(
        experiment_id=experiment_id,
        prolific_study_id=study_id,
        is_preview=is_preview,
        db=db,
    )
    # Round-level description (set per Prolific study) takes precedence so admins
    # can tailor each round; otherwise fall back to the dataset-level description.
    description_for_intro = round_description or experiment.description

    assistance_instructions = get_rater_instructions(experiment.assistance_method) or None

    existing_rater = await fetch_existing_rater_for_experiment(
        prolific_id=prolific_pid,
        experiment_id=experiment_id,
        db=db,
    )

    if existing_rater:
        if existing_rater.is_preview:
            # Reset preview rater so they can run through the flow again from scratch
            for rating in (
                await db.execute(select(Rating).where(Rating.rater_id == existing_rater.id))
            ).scalars():
                await db.delete(rating)
            for session in (
                await db.execute(
                    select(AssistanceSession).where(AssistanceSession.rater_id == existing_rater.id)
                )
            ).scalars():
                await db.delete(session)
            existing_rater.is_active = True
            existing_rater.session_start = datetime.now(UTC)
            existing_rater.session_end = None
            await db.commit()
            await db.refresh(existing_rater)
            logger.info(
                "Preview rater reset",
                extra={
                    "attributes": {
                        "rater_id": existing_rater.id,
                        "experiment_id": experiment_id,
                    }
                },
            )
            token = issue_rater_session_token(
                settings=settings, rater_id=existing_rater.id, experiment_id=experiment_id
            )
            return build_rater_start_response(
                rater_id=existing_rater.id,
                session_start=existing_rater.session_start,
                experiment_name=experiment.name,
                experiment_description=description_for_intro,
                human_prompt_prefix=experiment.human_prompt_prefix,
                human_prompt_suffix=experiment.human_prompt_suffix,
                completion_url=experiment.prolific_completion_url,
                rater_session_token=token,
                assistance_method=experiment.assistance_method,
                assistance_instructions=assistance_instructions,
            )
        validate_existing_rater_can_resume(existing_rater)
        token = issue_rater_session_token(
            settings=settings, rater_id=existing_rater.id, experiment_id=experiment_id
        )
        return build_rater_start_response(
            rater_id=existing_rater.id,
            session_start=existing_rater.session_start,
            experiment_name=experiment.name,
            experiment_description=description_for_intro,
            human_prompt_prefix=experiment.human_prompt_prefix,
            human_prompt_suffix=experiment.human_prompt_suffix,
            completion_url=experiment.prolific_completion_url,
            rater_session_token=token,
            assistance_method=experiment.assistance_method,
            assistance_instructions=assistance_instructions,
        )

    rater = Rater(
        prolific_id=prolific_pid,
        study_id=study_id,
        session_id=session_id,
        experiment_id=experiment_id,
        session_start=datetime.now(UTC),
        is_active=True,
        is_preview=is_preview,
    )
    db.add(rater)
    await db.commit()
    await db.refresh(rater)

    logger.info(
        "Rater session started",
        extra={
            "attributes": {
                "rater_id": rater.id,
                "experiment_id": experiment_id,
                "prolific_pid": prolific_pid,
                "is_preview": is_preview,
            }
        },
    )

    # Add to the experiment's Prolific participant group so later experiments
    # can blocklist them. Preview raters aren't real Prolific participants, so
    # skip them. Best-effort: never block rater entry on a Prolific failure.
    if not is_preview and settings.prolific.enabled:
        try:
            group_id = await ensure_participant_group_and_commit(experiment, db)
            if group_id:
                await add_participant_to_group(
                    settings=settings.prolific,
                    group_id=group_id,
                    prolific_id=prolific_pid,
                )
        except ProlificAPIError as exc:
            logger.warning(
                "Failed to add rater to Prolific participant group",
                extra={
                    "attributes": {
                        "rater_id": rater.id,
                        "experiment_id": experiment_id,
                        "prolific_status": exc.status_code,
                        "prolific_body": exc.body,
                    }
                },
            )
        except Exception:
            logger.warning(
                "Failed to add rater to Prolific participant group",
                exc_info=True,
                extra={
                    "attributes": {
                        "rater_id": rater.id,
                        "experiment_id": experiment_id,
                    }
                },
            )

    token = issue_rater_session_token(
        settings=settings, rater_id=rater.id, experiment_id=experiment_id
    )

    return build_rater_start_response(
        rater_id=rater.id,
        session_start=rater.session_start,
        experiment_name=experiment.name,
        experiment_description=description_for_intro,
        human_prompt_prefix=experiment.human_prompt_prefix,
        human_prompt_suffix=experiment.human_prompt_suffix,
        completion_url=experiment.prolific_completion_url,
        rater_session_token=token,
        assistance_method=experiment.assistance_method,
        assistance_instructions=assistance_instructions,
    )


# Namespace for pg_advisory_xact_lock so this feature's locks can't collide
# with any future advisory-lock use keyed on the same small integers.
_ASSIGNMENT_LOCK_NAMESPACE = 4217


async def _acquire_assignment_lock(experiment_id: int, db: AsyncSession) -> None:
    """Serialize question selection per experiment.

    Held until the surrounding transaction ends (the assignment commit, or
    request teardown on the re-serve path). Selection is a sub-second
    read-and-reserve, so contention is negligible next to minutes-long
    rating work.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _ASSIGNMENT_LOCK_NAMESPACE, "key": experiment_id},
    )


async def _reserve_question(
    *,
    rater_id: int,
    question_id: int,
    now: datetime,
    db: AsyncSession,
) -> None:
    """Create or revive this rater's reservation for a question.

    A stale row can exist when a prior assignment for the same question
    expired unanswered and the question is being served to the rater again;
    the unique constraint on (question_id, rater_id) means we update it in
    place. Caller holds the per-experiment advisory lock.
    """
    expires_at = now + timedelta(minutes=ASSIGNMENT_TTL_MINUTES)
    existing = await fetch_assignment_for_question(
        rater_id=rater_id, question_id=question_id, db=db
    )
    if existing:
        existing.assigned_at = now
        existing.expires_at = expires_at
        existing.completed_at = None
    else:
        db.add(
            QuestionAssignment(
                question_id=question_id,
                rater_id=rater_id,
                assigned_at=now,
                expires_at=expires_at,
            )
        )
    await db.commit()


async def get_next_question(
    *,
    rater_id: int,
    db: AsyncSession,
) -> Optional[QuestionResponse]:
    rater = await fetch_rater_or_404(rater_id, db)
    experiment = await fetch_experiment_or_404(rater.experiment_id, db)

    await validate_rater_session_is_active(rater, db)

    now = datetime.now(UTC)

    if not rater.is_preview:
        await _acquire_assignment_lock(rater.experiment_id, db)

        # Re-serve an outstanding reservation rather than picking fresh, so a
        # refresh can't re-roll the question or leak an extra reserved slot.
        live_assignment = await fetch_live_assignment_for_rater(rater_id=rater_id, now=now, db=db)
        if live_assignment is not None:
            question = await fetch_question_or_404(live_assignment.question_id, db)
            parent_text = (
                await fetch_parent_question_text(question.parent_question_id, db)
                if question.parent_question_id is not None
                else None
            )
            return build_question_response(question, parent_question_text=parent_text)

    rated_question_ids = await fetch_rated_question_ids(rater_id, db)
    eligible_questions = await fetch_eligible_questions_with_counts(
        experiment_id=rater.experiment_id,
        rated_question_ids=rated_question_ids,
        rater_id=rater_id,
        now=now,
        db=db,
    )
    in_progress_parent_ids = await fetch_in_progress_parent_ids(rater_id, db)

    open_questions, backfill_questions, done_questions = build_question_selection_groups(
        eligible_questions=eligible_questions,
        target_ratings_per_question=experiment.num_ratings_per_question,
    )
    selected = build_selected_question(
        open_questions=open_questions,
        backfill_questions=backfill_questions,
        done_questions=done_questions,
        in_progress_parent_ids=in_progress_parent_ids,
    )

    if selected is None:
        logger.info(
            "Rater has rated every question; ending their session",
            extra={
                "attributes": {
                    "rater_id": rater_id,
                    "experiment_id": rater.experiment_id,
                    "eligible_count": len(eligible_questions),
                }
            },
        )
        return None

    if not rater.is_preview:
        await _reserve_question(rater_id=rater_id, question_id=selected.id, now=now, db=db)

    parent_text = (
        await fetch_parent_question_text(selected.parent_question_id, db)
        if selected.parent_question_id is not None
        else None
    )
    return build_question_response(selected, parent_question_text=parent_text)


async def submit_rating(
    *,
    payload: RatingSubmit,
    rater_id: int,
    db: AsyncSession,
) -> RatingResponse:
    rater = await fetch_rater_or_404(rater_id, db)
    validate_rater_marked_active(rater)

    question = await fetch_question_or_404(payload.question_id, db)
    validate_question_belongs_to_rater_experiment(
        question_experiment_id=question.experiment_id,
        rater_experiment_id=rater.experiment_id,
    )

    existing_rating = await fetch_existing_rating(
        rater_id=rater_id,
        question_id=payload.question_id,
        db=db,
    )
    if existing_rating:
        raise HTTPException(status_code=400, detail="Already rated this question")

    validate_rating_confidence(payload.confidence)

    if payload.assistance_session_id is not None:
        assistance_session = (
            await db.execute(
                select(AssistanceSession).where(
                    AssistanceSession.id == payload.assistance_session_id
                )
            )
        ).scalar_one_or_none()
        if (
            assistance_session is None
            or assistance_session.rater_id != rater_id
            or assistance_session.question_id != payload.question_id
        ):
            raise HTTPException(
                status_code=400, detail="Invalid assistance_session_id for this rater and question"
            )

    now = datetime.now(UTC)
    db_rating = Rating(
        question_id=payload.question_id,
        rater_id=rater_id,
        answer=payload.answer,
        confidence=payload.confidence,
        time_started=_normalize_to_utc_aware(payload.time_started),
        time_submitted=now,
        assistance_session_id=payload.assistance_session_id,
    )
    db.add(db_rating)

    # Close out the reservation in the same commit as the rating, so the
    # slot is never counted twice (once as a live assignment, once as a
    # rating). A rating that arrives after its reservation expired is still
    # accepted — the rater did the work — even if that overshoots the
    # target; analysis truncates to the first N per question.
    assignment = await fetch_assignment_for_question(
        rater_id=rater_id, question_id=payload.question_id, db=db
    )
    if assignment is not None and assignment.completed_at is None:
        assignment.completed_at = now

    await db.commit()
    await db.refresh(db_rating)

    logger.info(
        "Rating submitted",
        extra={
            "attributes": {
                "rating_id": db_rating.id,
                "rater_id": rater_id,
                "experiment_id": rater.experiment_id,
                "question_id": payload.question_id,
                "question_type": question.question_type,
            }
        },
    )

    if not rater.is_preview:
        await _stop_rounds_if_target_met(experiment_id=rater.experiment_id, db=db)

    return RatingResponse(id=db_rating.id, success=True)


async def _stop_rounds_if_target_met(*, experiment_id: int, db: AsyncSession) -> None:
    """Stop this experiment's running Prolific studies once every question
    has its target ratings.

    Fires after a rating commit; best-effort — a Prolific failure never
    fails the submit, and the next qualifying submit (or a manual close)
    retries. Stopping promptly matters because each additional entrant costs
    a full fixed reward while only producing overshoot that analysis
    truncates away.
    """
    settings = get_settings()
    if not settings.prolific.enabled:
        return

    try:
        experiment = await fetch_experiment_or_404(experiment_id, db)
        remaining = await fetch_remaining_rating_actions(
            experiment_id=experiment_id,
            target_ratings_per_question=experiment.num_ratings_per_question,
            db=db,
        )
        if remaining > 0:
            return

        running_rounds = (
            (
                await db.execute(
                    select(ExperimentRound)
                    .where(ExperimentRound.experiment_id == experiment_id)
                    .where(
                        ExperimentRound.prolific_study_status.in_(
                            [ProlificStudyStatus.ACTIVE, ProlificStudyStatus.PAUSED]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for round_ in running_rounds:
            result = await stop_study(
                settings=settings.prolific,
                study_id=round_.prolific_study_id,
            )
            status = result.get("status")
            round_.prolific_study_status = (
                ProlificStudyStatus(status) if status else ProlificStudyStatus.AWAITING_REVIEW
            )
            logger.info(
                "Auto-stopped Prolific round: rating target met",
                extra={
                    "attributes": {
                        "experiment_id": experiment_id,
                        "round_id": round_.id,
                        "study_id": round_.prolific_study_id,
                    }
                },
            )
        if running_rounds:
            await db.commit()
    except Exception:
        logger.warning(
            "Failed to auto-stop Prolific rounds after target met",
            exc_info=True,
            extra={"attributes": {"experiment_id": experiment_id}},
        )


async def get_session_status(
    *,
    rater_id: int,
    db: AsyncSession,
) -> SessionStatusResponse:
    rater = await fetch_rater_or_404(rater_id, db)

    time_remaining = (
        build_session_end_time(rater.session_start) - datetime.now(UTC)
    ).total_seconds()
    if time_remaining <= 0:
        rater.is_active = False
        rater.session_end = datetime.now(UTC)
        await db.commit()
        time_remaining = 0

    completed = await fetch_rater_completed_count(rater_id, db)

    return SessionStatusResponse(
        is_active=rater.is_active,
        time_remaining_seconds=max(0, int(time_remaining)),
        questions_completed=completed,
    )


async def end_session(
    *,
    rater_id: int,
    db: AsyncSession,
) -> dict[str, str]:
    rater = await fetch_rater_or_404(rater_id, db)

    now = datetime.now(UTC)
    rater.is_active = False
    rater.session_end = now

    # Release any outstanding reservation right away rather than waiting for
    # its TTL, so the slot is immediately servable to other raters.
    live_assignment = await fetch_live_assignment_for_rater(rater_id=rater_id, now=now, db=db)
    if live_assignment is not None:
        live_assignment.expires_at = now

    await db.commit()

    return {"message": "Session ended successfully"}
