from __future__ import annotations

from datetime import datetime, timedelta

from models import Question, SESSION_DURATION_MINUTES
from schemas import QuestionResponse, RaterStartResponse


def build_session_end_time(session_start: datetime) -> datetime:
    return session_start + timedelta(minutes=SESSION_DURATION_MINUTES)


def build_question_response(
    question: Question,
    parent_question_text: str | None = None,
) -> QuestionResponse:
    return QuestionResponse(
        id=question.id,
        question_id=question.question_id,
        question_text=question.question_text,
        options=question.options,
        question_type=question.question_type,
        parent_question_text=parent_question_text,
    )


def build_rater_start_response(
    *,
    rater_id: int,
    session_start: datetime,
    experiment_name: str,
    completion_url: str | None,
    rater_session_token: str,
    assistance_method: str = "none",
) -> RaterStartResponse:
    return RaterStartResponse(
        rater_id=rater_id,
        session_start=session_start,
        session_end_time=build_session_end_time(session_start),
        experiment_name=experiment_name,
        completion_url=completion_url,
        rater_session_token=rater_session_token,
        assistance_method=assistance_method,
    )
