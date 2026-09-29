"""One active assignment and a bounded number of reserved successors per session."""

from datetime import UTC, datetime, timedelta
import json

from fastapi import HTTPException
from sqlalchemy import select, func

from config import get_settings
from models import (
    AssistancePreparation,
    AssistanceSession,
    QuestionAssignment,
    Rater,
    Rating,
    StepType,
)
from schemas import QueueItem, QueueRequest, QueueSnapshot
from services.assistance.preparation import PreparationContext, QuestionSnapshot
from services.assistance.registry import get_method
from services.queries import (
    fetch_experiment_or_404,
    fetch_question_or_404,
    fetch_parent_question_text,
)
from session_policy import resolve_session_policy
from .mappers import build_question_response
from .operations import _acquire_assignment_lock
from .queries import (
    fetch_eligible_questions_with_counts,
    fetch_in_progress_parent_ids,
    fetch_rated_question_ids,
)
from .selectors import build_question_selection_groups, build_selected_question
from .validators import validate_rater_marked_active, validate_rater_session_not_over


def enabled(experiment_id: int) -> bool:
    return experiment_id in get_settings().prefetch.experiment_ids


def speculation_enabled(experiment_id: int) -> bool:
    return enabled(experiment_id) and get_settings().prefetch.lookahead_questions > 0


async def lock_rater(rater_id, db):
    rater = await db.get(Rater, rater_id)
    if rater is None:
        raise HTTPException(404, "Rater not found")
    generation = db.info.get("authenticated_rater_generation", (rater.id, rater.session_start))
    await _acquire_assignment_lock(rater.experiment_id, db)
    locked = (
        await db.execute(
            select(Rater)
            .where(Rater.id == rater_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one()
    # Authentication read this same session before we waited for the lock.
    if (locked.id, locked.session_start) != generation:
        raise HTTPException(401, "Rater session was reset")
    return locked


async def current_assignments(rater_id, db):
    return list(
        (
            await db.execute(
                select(QuestionAssignment)
                .where(
                    QuestionAssignment.rater_id == rater_id,
                    QuestionAssignment.completed_at.is_(None),
                )
                .order_by(QuestionAssignment.assigned_at, QuestionAssignment.id)
            )
        ).scalars()
    )


async def release(assignment, db, now):
    assignment.completed_at = now
    assignment.expires_at = now
    for row in (
        await db.execute(
            select(AssistancePreparation).where(
                AssistancePreparation.assignment_id == assignment.id,
                AssistancePreparation.assignment_generation == assignment.generation,
                AssistancePreparation.status != "complete",
            )
        )
    ).scalars():
        row.status = "cancelled"
        row.owner_token = None
        row.claim_expires_at = None


async def snapshot(rater, experiment, assignments, db):
    now = datetime.now(UTC)
    policy = resolve_session_policy(experiment)
    items = []
    for assignment in assignments:
        question = await fetch_question_or_404(assignment.question_id, db)
        parent = (
            await fetch_parent_question_text(question.parent_question_id, db)
            if question.parent_question_id
            else None
        )
        items.append(
            QueueItem(
                assignment_id=assignment.id,
                generation=assignment.generation,
                activated=assignment.activated_at is not None,
                question=build_question_response(
                    question, is_markdown=experiment.is_markdown, parent_question_text=parent
                ),
            )
        )
    return QueueSnapshot(
        session_generation=rater.session_start.isoformat(),
        revision=rater.queue_revision,
        phase="ended"
        if not rater.is_active or now > policy.hard_deadline(rater.session_start)
        else "grace"
        if now > policy.deadline(rater.session_start)
        else "active",
        prefetch_enabled=speculation_enabled(experiment.id),
        items=items,
    )


async def skipped_question_ids(rater, db):
    closed = (
        (
            await db.execute(
                select(QuestionAssignment.question_id)
                .join(
                    AssistanceSession,
                    AssistanceSession.question_id == QuestionAssignment.question_id,
                )
                .where(
                    QuestionAssignment.rater_id == rater.id,
                    QuestionAssignment.completed_at >= rater.session_start,
                    AssistanceSession.rater_id == rater.id,
                    AssistanceSession.step_type == StepType.SKIP,
                )
            )
        )
        .scalars()
        .all()
    )
    return list(closed)


async def reserve_question(rater, question_id, policy, now, db):
    assignment = (
        await db.execute(
            select(QuestionAssignment).where(
                QuestionAssignment.rater_id == rater.id,
                QuestionAssignment.question_id == question_id,
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        assignment = QuestionAssignment(
            rater_id=rater.id, question_id=question_id, assigned_at=now, expires_at=now
        )
        db.add(assignment)
    else:
        assignment.generation += 1
        assignment.assigned_at = now
        assignment.completed_at = None
        assignment.activated_at = None
    assignment.expires_at = min(
        now + timedelta(minutes=policy.assignment_ttl_minutes),
        policy.deadline(rater.session_start),
    )
    await db.flush()
    return assignment


async def replace_completed_head(rater, experiment, selected, assignments, policy, now, db):
    """Keep prepared work unless its submitted target is met and work remains."""
    # Preview navigation is explicit and must not follow production coverage.
    if rater.is_preview:
        return selected
    committed = await db.scalar(
        select(func.count(Rating.id))
        .join(Rater, Rating.rater_id == Rater.id)
        .where(Rating.question_id == selected.question_id, Rater.is_preview.is_(False))
    )
    target = experiment.num_ratings_per_question
    if committed < target:
        return selected
    excluded = await fetch_rated_question_ids(rater.id, db)
    excluded.extend(await skipped_question_ids(rater, db))
    candidates = await fetch_eligible_questions_with_counts(
        experiment_id=experiment.id,
        rated_question_ids=excluded,
        rater_id=rater.id,
        now=now,
        db=db,
    )
    # Keep queued alternatives eligible: their reservation and prepared work
    # can be reused. Only unfinished questions may displace this head, even
    # when a completed sibling would otherwise win on parent continuity.
    groups = build_question_selection_groups(
        eligible_questions=candidates,
        target_ratings_per_question=target,
    )
    parents = await fetch_in_progress_parent_ids(rater.id, db)
    head_question = await fetch_question_or_404(selected.question_id, db)
    if head_question.parent_question_id:
        parents.add(head_question.parent_question_id)
    question = build_selected_question(
        open_questions=groups[0],
        backfill_questions=groups[1],
        done_questions=[],
        in_progress_parent_ids=parents,
    )
    if question is None:
        return selected
    replacement = next((a for a in assignments if a.question_id == question.id), None)
    if replacement is None:
        replacement = await reserve_question(rater, question.id, policy, now, db)
    else:
        assignments.remove(replacement)
    await release(selected, db, now)
    assignments.remove(selected)
    assignments.insert(0, replacement)
    return replacement


async def queue_action(*, rater_id, body: QueueRequest, db):
    rater = await lock_rater(rater_id, db)
    experiment = await fetch_experiment_or_404(rater.experiment_id, db)
    policy = resolve_session_policy(experiment)
    validate_rater_marked_active(rater)
    await validate_rater_session_not_over(rater, db, policy)
    # This request negotiates client support. A queue-enabled session may have
    # waited on its intro screen while the allowlist changed; let it enter at
    # depth one. The allowlist controls offers at /start and speculative work.
    rater.queue_mode = True
    now = datetime.now(UTC)
    assignments = await current_assignments(rater_id, db)
    # Legacy assignments were already displayed; keep only the newest one.
    active = [a for a in assignments if a.activated_at is not None]
    for a in assignments[:]:
        if (
            a.activated_at is None
            and (a.expires_at <= now or now > policy.deadline(rater.session_start))
        ) or (a in active[:-1]):
            await release(a, db, now)
            assignments.remove(a)
            rater.queue_revision += 1
    assignments.sort(key=lambda a: (a.activated_at is None, a.assigned_at, a.id))

    if body.action != "reserve":
        selected = next(
            (
                a
                for a in assignments
                if a.id == body.assignment_id and a.generation == body.generation
            ),
            None,
        )
        # An activation retry is idempotent even if refill advanced the revision.
        already_active = (
            body.action == "activate" and selected is not None and selected.activated_at is not None
        )
        if not already_active and (
            body.revision != rater.queue_revision or selected is None or selected != assignments[0]
        ):
            result = await snapshot(rater, experiment, assignments, db)
            await db.commit()
            raise HTTPException(
                409, {"message": "Queue changed", "queue": result.model_dump(mode="json")}
            )
        if body.action == "activate" and not already_active:
            if now > policy.deadline(rater.session_start):
                raise HTTPException(403, "Session expired")
            selected = await replace_completed_head(
                rater, experiment, selected, assignments, policy, now, db
            )
            selected.activated_at = now
            rater.queue_revision += 1
        elif body.action == "skip":
            session = (
                await db.execute(
                    select(AssistanceSession).where(
                        AssistanceSession.rater_id == rater_id,
                        AssistanceSession.question_id == selected.question_id,
                    )
                )
            ).scalar_one_or_none()
            if (
                selected.activated_at is None
                or session is None
                or session.step_type != StepType.SKIP
            ):
                raise HTTPException(409, "Only a failed assistance step can be skipped")
            await release(selected, db, now)
            assignments.remove(selected)
            rater.queue_revision += 1

    if body.action == "reserve" and now <= policy.deadline(rater.session_start):
        if body.pinned_question_id is not None:
            if not rater.is_preview:
                raise HTTPException(403, "Only preview sessions can open a specific question")
            # Pins are initial navigation, never a replacement for active work.
            if assignments and assignments[0].question_id != body.pinned_question_id:
                raise HTTPException(409, "Finish the active question before opening a pin")
        depth = 1 + get_settings().prefetch.lookahead_questions if enabled(experiment.id) else 1
        excluded = await fetch_rated_question_ids(rater_id, db)
        excluded.extend(await skipped_question_ids(rater, db))
        parents = await fetch_in_progress_parent_ids(rater_id, db)
        for assignment in assignments:
            q = await fetch_question_or_404(assignment.question_id, db)
            if q.parent_question_id:
                parents.add(q.parent_question_id)
        while len(assignments) < depth:
            candidates = await fetch_eligible_questions_with_counts(
                experiment_id=experiment.id,
                rated_question_ids=excluded + [a.question_id for a in assignments],
                rater_id=rater_id,
                now=now,
                db=db,
            )
            if body.pinned_question_id is not None and not assignments:
                selected = next(
                    (q for q, _, _ in candidates if q.id == body.pinned_question_id), None
                )
                if selected is None:
                    raise HTTPException(404, "Question is not available in this experiment")
            else:
                groups = build_question_selection_groups(
                    eligible_questions=candidates,
                    target_ratings_per_question=experiment.num_ratings_per_question,
                )
                selected = build_selected_question(
                    open_questions=groups[0],
                    backfill_questions=groups[1],
                    done_questions=groups[2],
                    in_progress_parent_ids=parents,
                )
            if selected is None:
                break
            method = get_method(experiment.assistance_method)
            params = method.preparation_params(json.loads(experiment.assistance_params or "{}"))
            parent = (
                await fetch_parent_question_text(selected.parent_question_id, db)
                if selected.parent_question_id
                else None
            )
            context = PreparationContext(
                QuestionSnapshot.capture(selected),
                json.dumps(params),
                parent,
                experiment.system_prompt,
            )
            if method.plan_preparation(context) is None:
                depth = 1
                if assignments:
                    break
            assignment = await reserve_question(rater, selected.id, policy, now, db)
            assignments.append(assignment)
            if selected.parent_question_id:
                parents.add(selected.parent_question_id)
            rater.queue_revision += 1
    result = await snapshot(rater, experiment, assignments, db)
    await db.commit()
    return result


async def require_assignment(
    rater, question_id, db, *, active=True, generation=None, identifier=None
):
    assignment = (
        await db.execute(
            select(QuestionAssignment).where(
                QuestionAssignment.rater_id == rater.id,
                QuestionAssignment.question_id == question_id,
                QuestionAssignment.completed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if (
        assignment is None
        or (identifier is not None and assignment.id != identifier)
        or (generation is not None and assignment.generation != generation)
        or (active and assignment.activated_at is None)
    ):
        raise HTTPException(409, "Question is not the active assignment")
    if (
        not active
        and assignment.activated_at is None
        and assignment.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Reservation expired")
    return assignment
