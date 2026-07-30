from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models import Experiment, ExperimentRound, ExperimentStatus, Question, Rating, Rater, Upload
from schemas import ExperimentCreate, ExperimentResponse, ExperimentUpdate
from .mappers import build_experiment_response
from fastapi import HTTPException
from .prolific import delete_study
from .status import assert_can_finish, compute_attention_reason, is_locked
from services.assistance.registry import get_method
from services.queries import parent_question_ids_subquery
from .queries import (
    fetch_experiment_or_404,
    fetch_total_questions_for_experiment,
    fetch_total_ratings_for_experiment,
)

logger = logging.getLogger(__name__)


async def create_experiment(
    payload: ExperimentCreate,
    db: AsyncSession,
) -> ExperimentResponse:
    try:
        get_method(payload.assistance_method)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    db_experiment = Experiment(
        name=payload.name,
        internal_name=(payload.internal_name.strip() or None) if payload.internal_name else None,
        num_ratings_per_question=payload.num_ratings_per_question,
        prolific_completion_url=payload.prolific_completion_url,
        assistance_method=payload.assistance_method,
        assistance_params=json.dumps(payload.assistance_params)
        if payload.assistance_params
        else None,
    )
    db.add(db_experiment)
    await db.commit()
    await db.refresh(db_experiment)

    logger.info(
        "Experiment created",
        extra={
            "attributes": {
                "experiment_id": db_experiment.id,
                "experiment_name": db_experiment.name,
            }
        },
    )
    return build_experiment_response(db_experiment, question_count=0, rating_count=0)


async def list_experiments(
    skip: int,
    limit: int,
    db: AsyncSession,
    archived: bool = False,
    include_archived: bool = False,
    status: ExperimentStatus | None = None,
    search: str | None = None,
    ids: list[int] | None = None,
) -> list[ExperimentResponse]:
    question_counts = (
        select(
            Question.experiment_id,
            func.count(Question.id).label("question_count"),
        )
        .where(Question.id.notin_(parent_question_ids_subquery()))
        .group_by(Question.experiment_id)
        .subquery()
    )

    rating_counts = (
        select(
            Question.experiment_id,
            func.count(Rating.id).label("rating_count"),
        )
        .join(Rating, Rating.question_id == Question.id)
        .group_by(Question.experiment_id)
        .subquery()
    )

    stmt = (
        select(
            Experiment,
            func.coalesce(question_counts.c.question_count, 0).label("question_count"),
            func.coalesce(rating_counts.c.rating_count, 0).label("rating_count"),
        )
        .outerjoin(question_counts, Experiment.id == question_counts.c.experiment_id)
        .outerjoin(rating_counts, Experiment.id == rating_counts.c.experiment_id)
    )

    # An explicit id set is a targeted batch fetch: return exactly those rows
    # regardless of archived state (the caller already knows what it wants).
    if ids is not None:
        stmt = stmt.where(Experiment.id.in_(ids))

    # `include_archived` (used by the client-side-filtered dashboard) returns
    # both active and archived rows; otherwise the `archived` flag selects one.
    # Skipped for id-batch fetches so an archived id in the set still resolves.
    if ids is None and not include_archived:
        stmt = stmt.where(
            Experiment.archived_at.is_not(None) if archived else Experiment.archived_at.is_(None)
        )

    if status is not None:
        stmt = stmt.where(Experiment.status == status)

    # Case-insensitive substring match against either the public or internal
    # name. `%`/`_` are escaped so a literal search term can't act as a wildcard.
    if search and search.strip():
        term = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{term}%"
        stmt = stmt.where(
            or_(
                Experiment.name.ilike(pattern, escape="\\"),
                Experiment.internal_name.ilike(pattern, escape="\\"),
            )
        )

    rows = (
        await db.execute(stmt.order_by(Experiment.created_at.desc()).offset(skip).limit(limit))
    ).all()

    return await _build_experiment_responses(list(rows), db)


async def _build_experiment_responses(
    rows: list[tuple[Experiment, int, int]],
    db: AsyncSession,
) -> list[ExperimentResponse]:
    """Enrich (experiment, question_count, rating_count) rows with dataset
    filenames, spend, and the attention flag.

    The per-experiment queries are keyed on the batch of ids rather than
    joined into the caller's select, so the fan-out stays bounded. Shared by
    ``list_experiments`` and ``get_experiment`` so a single experiment and a
    listed one return the same shape.
    """
    experiment_ids = [experiment.id for experiment, _, _ in rows]
    filenames_by_experiment: dict[int, list[str]] = {eid: [] for eid in experiment_ids}
    # Per-experiment shortfall against the rating target and the set of round
    # statuses — both feed the "needs attention" flag below.
    remaining_by_experiment: dict[int, int] = {}
    round_statuses_by_experiment: dict[int, list[str]] = {eid: [] for eid in experiment_ids}
    # Experiment spend = Σ over rounds of Prolific's own `total_cost` (minor
    # units), captured on sync. Rounds never synced contribute nothing.
    spend_by_experiment: dict[int, int] = {}
    if experiment_ids:
        upload_rows = (
            await db.execute(
                select(Upload.experiment_id, Upload.filename)
                .where(Upload.experiment_id.in_(experiment_ids))
                .order_by(Upload.uploaded_at)
            )
        ).all()
        for exp_id, filename in upload_rows:
            filenames_by_experiment.setdefault(exp_id, []).append(filename)

        # remaining = Σ max(0, target − ratings) over each non-parent question,
        # matching calculate_recommendation's definition of "target not met".
        # Preview ratings are excluded — they aren't real data and must not
        # make the target look met.
        real_ratings = (
            select(Rating.id, Rating.question_id)
            .join(Rater, Rating.rater_id == Rater.id)
            .where(Rater.is_preview == False)  # noqa: E712
            .subquery()
        )
        per_question_counts = (
            select(
                Question.experiment_id.label("experiment_id"),
                Question.id.label("question_id"),
                func.count(real_ratings.c.id).label("cnt"),
            )
            .outerjoin(real_ratings, real_ratings.c.question_id == Question.id)
            .where(Question.experiment_id.in_(experiment_ids))
            .where(Question.id.notin_(parent_question_ids_subquery()))
            .group_by(Question.experiment_id, Question.id)
            .subquery()
        )
        deficit = Experiment.num_ratings_per_question - per_question_counts.c.cnt
        clamped_deficit = case((deficit < 0, 0), else_=deficit)
        remaining_rows = (
            await db.execute(
                select(
                    per_question_counts.c.experiment_id,
                    func.coalesce(func.sum(clamped_deficit), 0),
                )
                .join(Experiment, Experiment.id == per_question_counts.c.experiment_id)
                .group_by(per_question_counts.c.experiment_id)
            )
        ).all()
        remaining_by_experiment = {exp_id: int(rem or 0) for exp_id, rem in remaining_rows}

        status_rows = (
            await db.execute(
                select(
                    ExperimentRound.experiment_id,
                    ExperimentRound.prolific_study_status,
                    ExperimentRound.total_cost,
                ).where(ExperimentRound.experiment_id.in_(experiment_ids))
            )
        ).all()
        for exp_id, study_status, total_cost in status_rows:
            round_statuses_by_experiment.setdefault(exp_id, []).append(study_status)
            if total_cost:
                spend_by_experiment[exp_id] = spend_by_experiment.get(exp_id, 0) + int(total_cost)

    return [
        build_experiment_response(
            experiment,
            question_count=int(question_count or 0),
            rating_count=int(rating_count or 0),
            dataset_filenames=filenames_by_experiment.get(experiment.id, []),
            attention_reason=compute_attention_reason(
                status=experiment.status,
                remaining_actions=remaining_by_experiment.get(experiment.id, 0),
                round_statuses=round_statuses_by_experiment.get(experiment.id, []),
            ),
            spend_minor_units=spend_by_experiment.get(experiment.id, 0),
        )
        for experiment, question_count, rating_count in rows
    ]


async def get_experiment(
    experiment_id: int,
    db: AsyncSession,
) -> ExperimentResponse:
    """Fetch a single experiment by id, regardless of archived state.

    The list endpoint hides archived rows, so the detail view needs a direct
    lookup to open one. Returns the same enriched shape as a list row.
    """
    experiment = await fetch_experiment_or_404(experiment_id, db)
    question_count = await fetch_total_questions_for_experiment(experiment_id, db)
    rating_count = await fetch_total_ratings_for_experiment(experiment_id, db)
    responses = await _build_experiment_responses([(experiment, question_count, rating_count)], db)
    return responses[0]


_LOCKED_META_FIELDS = (
    "description",
    "system_prompt",
    "human_prompt_prefix",
    "human_prompt_suffix",
    "prolific_pool",
)

# Matches the String(255) columns on Experiment.name / internal_name.
_NAME_MAX_LENGTH = 255


def _with_copy_suffix(base: str, suffix: str) -> str:
    """Append suffix, trimming the base so the result fits the name columns."""
    return base[: _NAME_MAX_LENGTH - len(suffix)].rstrip() + suffix


def _pick_copy_names(
    name: str,
    internal_name: str | None,
    existing_names: set[str],
    existing_internal_names: set[str],
) -> tuple[str, str | None]:
    """Choose "<base> COPY" names for a duplicate, bumping to "COPY (n)" on clashes.

    One shared n is used for both names so the public and internal labels of a
    duplicate never end up with mismatched suffixes.
    """
    n = 1
    while True:
        suffix = " COPY" if n == 1 else f" COPY ({n})"
        candidate_name = _with_copy_suffix(name, suffix)
        candidate_internal = _with_copy_suffix(internal_name, suffix) if internal_name else None
        if candidate_name not in existing_names and (
            candidate_internal is None or candidate_internal not in existing_internal_names
        ):
            return candidate_name, candidate_internal
        n += 1


async def duplicate_experiment(
    experiment_id: int,
    db: AsyncSession,
) -> ExperimentResponse:
    """Create a fresh DRAFT experiment from an existing one.

    Copied: names (with a " COPY" suffix), ratings per question, the dataset
    (questions + upload provenance), and the instructions-page fields
    (description, system prompt, prefix/suffix, prolific pool). Everything
    else — status, assistance config, rounds, raters, ratings, participant
    group — starts from defaults.
    """
    source = await fetch_experiment_or_404(experiment_id, db)

    existing_names = set((await db.execute(select(Experiment.name))).scalars().all())
    existing_internal_names = {
        value
        for value in (await db.execute(select(Experiment.internal_name))).scalars().all()
        if value
    }
    name, internal_name = _pick_copy_names(
        source.name, source.internal_name, existing_names, existing_internal_names
    )

    duplicate = Experiment(
        name=name,
        internal_name=internal_name,
        num_ratings_per_question=source.num_ratings_per_question,
        description=source.description,
        system_prompt=source.system_prompt,
        human_prompt_prefix=source.human_prompt_prefix,
        human_prompt_suffix=source.human_prompt_suffix,
        prolific_pool=source.prolific_pool,
    )
    db.add(duplicate)
    await db.flush()

    source_questions = (
        (
            await db.execute(
                select(Question)
                .where(Question.experiment_id == experiment_id)
                .order_by(Question.id)
            )
        )
        .scalars()
        .all()
    )
    cloned_questions = [
        Question(
            experiment_id=duplicate.id,
            question_id=question.question_id,
            question_text=question.question_text,
            gt_answer=question.gt_answer,
            options=question.options,
            question_type=question.question_type,
            extra_data=question.extra_data,
        )
        for question in source_questions
    ]
    for question in cloned_questions:
        db.add(question)
    # Flush so clones have DB ids, then remap parent references onto them.
    await db.flush()
    clone_id_by_source_id = {
        source_question.id: clone.id
        for source_question, clone in zip(source_questions, cloned_questions)
    }
    for source_question, clone in zip(source_questions, cloned_questions):
        if source_question.parent_question_id is not None:
            clone.parent_question_id = clone_id_by_source_id.get(source_question.parent_question_id)

    # Carry over upload provenance so the Questions tab of the copy shows where
    # its dataset came from (uploaded_at reflects duplication time).
    source_uploads = (
        (
            await db.execute(
                select(Upload)
                .where(Upload.experiment_id == experiment_id)
                .order_by(Upload.uploaded_at)
            )
        )
        .scalars()
        .all()
    )
    for upload in source_uploads:
        db.add(
            Upload(
                experiment_id=duplicate.id,
                filename=upload.filename,
                question_count=upload.question_count,
                dataset_meta=upload.dataset_meta,
            )
        )

    await db.commit()
    await db.refresh(duplicate)

    logger.info(
        "Experiment duplicated",
        extra={
            "attributes": {
                "source_experiment_id": experiment_id,
                "experiment_id": duplicate.id,
                "experiment_name": duplicate.name,
                "question_count": len(cloned_questions),
            }
        },
    )

    question_count = await fetch_total_questions_for_experiment(duplicate.id, db)
    return build_experiment_response(
        duplicate,
        question_count=question_count,
        rating_count=0,
        dataset_filenames=[upload.filename for upload in source_uploads],
    )


def _collect_locked_field_changes(experiment: Experiment, payload: ExperimentUpdate) -> list[str]:
    """Names of locked-experiment fields whose payload value would change the row.

    Callers use this to reject a PATCH that mutates a locked field once the
    experiment is past DRAFT. Fields that match the current value are treated as
    no-ops — the frontend commonly re-sends unchanged fields alongside the one
    edit it wants (e.g. dataset-meta save also re-sends assistance_method), and
    those pass-through sends shouldn't spuriously trip the lock.
    """
    changes: list[str] = []
    if payload.assistance_method != experiment.assistance_method:
        changes.append("assistance_method")
    # `assistance_params is None` is "leave unchanged" in the update path
    # (mirrors the meta-field loop below), so treat it as a no-op here too —
    # otherwise a PATCH that omits the field trips the lock on any experiment
    # that has params set.
    if payload.assistance_params is not None:
        current_params = (
            json.loads(experiment.assistance_params) if experiment.assistance_params else None
        )
        if payload.assistance_params != current_params:
            changes.append("assistance_params")
    for field_name in _LOCKED_META_FIELDS:
        proposed = getattr(payload, field_name)
        if proposed is None:
            continue
        normalized = proposed.strip() or None
        if normalized != getattr(experiment, field_name):
            changes.append(field_name)
    return changes


async def update_experiment(
    experiment_id: int,
    payload: ExperimentUpdate,
    db: AsyncSession,
) -> ExperimentResponse:
    try:
        get_method(payload.assistance_method)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    experiment = await fetch_experiment_or_404(experiment_id, db)

    if is_locked(experiment):
        locked_changes = _collect_locked_field_changes(experiment, payload)
        if locked_changes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot edit {', '.join(locked_changes)}: experiment is "
                    f"{experiment.status}. Config is locked once the first "
                    "main round is launched."
                ),
            )

    experiment.assistance_method = payload.assistance_method

    if payload.assistance_params is not None:
        experiment.assistance_params = json.dumps(payload.assistance_params)
    if payload.name is not None:
        stripped_name = payload.name.strip()
        if not stripped_name:
            raise HTTPException(status_code=400, detail="Public name cannot be empty.")
        experiment.name = stripped_name
    if payload.internal_name is not None:
        experiment.internal_name = payload.internal_name.strip() or None
    # For each dataset-meta field, None means "leave unchanged"; an explicit
    # empty string clears the field. This keeps PATCHy edits straightforward
    # from the admin UI (only send what the user touched).
    for field_name in _LOCKED_META_FIELDS:
        value = getattr(payload, field_name)
        if value is None:
            continue
        stripped = value.strip()
        setattr(experiment, field_name, stripped or None)

    await db.commit()
    await db.refresh(experiment)

    question_count = await fetch_total_questions_for_experiment(experiment_id, db)
    rating_count = await fetch_total_ratings_for_experiment(experiment_id, db)
    return build_experiment_response(
        experiment, question_count=question_count, rating_count=rating_count
    )


async def finish_experiment(
    experiment_id: int,
    db: AsyncSession,
) -> ExperimentResponse:
    experiment = await fetch_experiment_or_404(experiment_id, db)
    await assert_can_finish(experiment, db)

    experiment.status = ExperimentStatus.FINISHED
    await db.commit()
    await db.refresh(experiment)

    logger.info(
        "Experiment marked finished",
        extra={"attributes": {"experiment_id": experiment_id}},
    )

    question_count = await fetch_total_questions_for_experiment(experiment_id, db)
    rating_count = await fetch_total_ratings_for_experiment(experiment_id, db)
    return build_experiment_response(
        experiment, question_count=question_count, rating_count=rating_count
    )


async def _set_archived(
    experiment_id: int,
    db: AsyncSession,
    archived_at: datetime | None,
) -> ExperimentResponse:
    """Soft-archive transition. Non-destructive and reversible; unlike
    delete it leaves all child rows and Prolific studies intact."""
    experiment = await fetch_experiment_or_404(experiment_id, db)

    experiment.archived_at = archived_at
    await db.commit()
    await db.refresh(experiment)

    logger.info(
        "Experiment archived" if archived_at else "Experiment unarchived",
        extra={"attributes": {"experiment_id": experiment_id}},
    )

    question_count = await fetch_total_questions_for_experiment(experiment_id, db)
    rating_count = await fetch_total_ratings_for_experiment(experiment_id, db)
    return build_experiment_response(
        experiment, question_count=question_count, rating_count=rating_count
    )


async def archive_experiment(
    experiment_id: int,
    db: AsyncSession,
) -> ExperimentResponse:
    return await _set_archived(experiment_id, db, datetime.now(timezone.utc))


async def unarchive_experiment(
    experiment_id: int,
    db: AsyncSession,
) -> ExperimentResponse:
    return await _set_archived(experiment_id, db, None)


_PROLIFIC_CLEANUP_TIMEOUT_SECONDS = 3.0


async def delete_experiment(
    experiment_id: int,
    db: AsyncSession,
) -> dict[str, str]:
    settings = get_settings()
    experiment = await fetch_experiment_or_404(experiment_id, db)
    experiment_name = experiment.name

    # Snapshot linked Prolific study IDs before deleting the local rows.
    round_study_ids: list[str] = []
    if settings.prolific.enabled:
        round_study_ids = list(
            (
                await db.execute(
                    select(ExperimentRound.prolific_study_id).where(
                        ExperimentRound.experiment_id == experiment_id
                    )
                )
            )
            .scalars()
            .all()
        )

    await db.delete(experiment)
    await db.commit()

    logger.info(
        "Experiment deleted",
        extra={
            "attributes": {
                "experiment_id": experiment_id,
                "experiment_name": experiment_name,
            }
        },
    )

    # Best-effort Prolific cleanup: fired in parallel and bounded to a short
    # timeout per study. The local delete already committed above, so any
    # Prolific-side failure just leaves an orphan study for the researcher to
    # clean up manually from the Prolific dashboard. This keeps the response
    # snappy even when Prolific is slow or unreachable (their DELETE calls
    # can take ~30s in the worst case).
    if round_study_ids:
        await asyncio.gather(
            *(
                _delete_prolific_study_best_effort(settings.prolific, study_id)
                for study_id in round_study_ids
            ),
        )

    return {"message": "Experiment deleted successfully"}


async def _delete_prolific_study_best_effort(prolific_settings: Any, study_id: str) -> None:
    try:
        await asyncio.wait_for(
            delete_study(settings=prolific_settings, study_id=study_id),
            timeout=_PROLIFIC_CLEANUP_TIMEOUT_SECONDS,
        )
        logger.info(
            "Prolific study deleted",
            extra={"attributes": {"study_id": study_id}},
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Prolific study delete timed out; leaving orphan on Prolific",
            extra={"attributes": {"study_id": study_id}},
        )
    except Exception:
        logger.warning(
            "Failed to delete Prolific study after local delete",
            exc_info=True,
            extra={"attributes": {"study_id": study_id}},
        )


async def get_experiment_stats(
    experiment_id: int,
    db: AsyncSession,
    *,
    include_preview: bool = False,
) -> dict[str, Any]:
    experiment = await fetch_experiment_or_404(experiment_id, db)

    total_questions = await fetch_total_questions_for_experiment(experiment_id, db)

    ratings_stmt = (
        select(func.count(Rating.id))
        .join(Question, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .where(Question.experiment_id == experiment_id)
    )
    raters_stmt = select(func.count(Rater.id)).where(Rater.experiment_id == experiment_id)
    complete_stmt = (
        select(Question.id)
        .join(Rating, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .where(Question.experiment_id == experiment_id)
        .where(Question.id.notin_(parent_question_ids_subquery()))
        .group_by(Question.id)
        .having(func.count(Rating.id) >= experiment.num_ratings_per_question)
    )

    # Per-question counts capped at the target: overshoot on one question
    # must not offset a shortfall on another, so progress toward the target
    # sums min(count, target) rather than raw ratings. Parent rows are
    # excluded to match the total_questions denominator; they are never
    # served so this is defensive only.
    per_question_stmt = (
        select(func.count(Rating.id).label("count"))
        .join(Question, Rating.question_id == Question.id)
        .join(Rater, Rating.rater_id == Rater.id)
        .where(Question.experiment_id == experiment_id)
        .where(Question.id.notin_(parent_question_ids_subquery()))
        .group_by(Rating.question_id)
    )

    if not include_preview:
        preview_filter = Rater.is_preview == False  # noqa: E712
        ratings_stmt = ratings_stmt.where(preview_filter)
        raters_stmt = raters_stmt.where(preview_filter)
        complete_stmt = complete_stmt.where(preview_filter)
        per_question_stmt = per_question_stmt.where(preview_filter)

    per_question_counts = per_question_stmt.subquery()
    effective_stmt = select(
        func.coalesce(
            func.sum(func.least(per_question_counts.c.count, experiment.num_ratings_per_question)),
            0,
        )
    ).select_from(per_question_counts)

    total_ratings = (await db.execute(ratings_stmt)).scalar_one()
    total_raters = (await db.execute(raters_stmt)).scalar_one()
    questions_complete = len((await db.execute(complete_stmt)).all())
    effective_ratings = (await db.execute(effective_stmt)).scalar_one()

    return {
        "experiment_name": experiment.name,
        "total_questions": total_questions,
        "questions_complete": int(questions_complete),
        "total_ratings": int(total_ratings or 0),
        "effective_ratings": int(effective_ratings or 0),
        "total_raters": int(total_raters or 0),
        "target_ratings_per_question": experiment.num_ratings_per_question,
    }
