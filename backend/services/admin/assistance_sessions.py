"""Admin read access to assistance sessions and their append-only event log.

See docs/assistance-events.md for what the event rows mean. These endpoints
exist so a failed or surprising session can be reconstructed without a
database shell; they never write.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import AssistanceEvent, AssistanceSession
from schemas import (
    AssistanceEventResponse,
    AssistanceSessionDetail,
    AssistanceSessionResponse,
)
from services.queries import fetch_experiment_or_404, load_json_column


def _session_fields(session: AssistanceSession, event_count: int) -> dict:
    return {
        "id": session.id,
        "rater_id": session.rater_id,
        "experiment_id": session.experiment_id,
        "question_id": session.question_id,
        "method_name": session.method_name,
        "params": load_json_column(session.params),
        "step_type": session.step_type,
        "is_complete": session.is_complete,
        "turn": session.turn,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "event_count": event_count,
    }


async def list_assistance_sessions(
    *,
    experiment_id: int,
    rater_id: int | None,
    question_id: int | None,
    step_type: str | None,
    skip: int,
    limit: int,
    db: AsyncSession,
) -> list[AssistanceSessionResponse]:
    await fetch_experiment_or_404(experiment_id, db)

    event_count = func.count(AssistanceEvent.id)
    query = (
        select(AssistanceSession, event_count)
        .outerjoin(AssistanceEvent, AssistanceEvent.assistance_session_id == AssistanceSession.id)
        .where(AssistanceSession.experiment_id == experiment_id)
    )
    if rater_id is not None:
        query = query.where(AssistanceSession.rater_id == rater_id)
    if question_id is not None:
        query = query.where(AssistanceSession.question_id == question_id)
    if step_type is not None:
        query = query.where(AssistanceSession.step_type == step_type)
    rows = (
        await db.execute(
            query.group_by(AssistanceSession.id)
            .order_by(AssistanceSession.updated_at.desc(), AssistanceSession.id.desc())
            .offset(skip)
            .limit(limit)
        )
    ).all()
    return [
        AssistanceSessionResponse(**_session_fields(session, int(count))) for session, count in rows
    ]


async def get_assistance_session(*, session_id: int, db: AsyncSession) -> AssistanceSessionDetail:
    session = (
        await db.execute(select(AssistanceSession).where(AssistanceSession.id == session_id))
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Assistance session not found")

    events = (
        (
            await db.execute(
                select(AssistanceEvent)
                .where(AssistanceEvent.assistance_session_id == session_id)
                .order_by(AssistanceEvent.id)
            )
        )
        .scalars()
        .all()
    )
    return AssistanceSessionDetail(
        **_session_fields(session, len(events)),
        payload=load_json_column(session.payload),
        events=[
            AssistanceEventResponse(
                id=event.id,
                created_at=event.created_at,
                step_type=event.step_type,
                latency_ms=event.latency_ms,
                payload=load_json_column(event.payload),
                error=event.error,
            )
            for event in events
        ],
    )
