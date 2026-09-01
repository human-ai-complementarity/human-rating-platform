from __future__ import annotations

from datetime import datetime, timedelta

from models import Question, SESSION_DURATION_MINUTES
from schemas import QuestionResponse, RaterStartResponse
from services.prolific_markdown import to_prolific_html


def build_session_end_time(session_start: datetime) -> datetime:
    return session_start + timedelta(minutes=SESSION_DURATION_MINUTES)


def build_question_response(
    question: Question,
    parent_question_text: str | None = None,
) -> QuestionResponse:
    return QuestionResponse(
        id=question.id,
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
    experiment_description: str | None,
    human_prompt_prefix: str | None,
    human_prompt_suffix: str | None,
    completion_url: str | None,
    rater_session_token: str,
    assistance_method: str = "none",
    assistance_instructions: str | None = None,
) -> RaterStartResponse:
    # Render the description through the same converter used for the Prolific
    # study description, so what raters see matches what participants see on
    # Prolific's listing. Empty/whitespace markdown produces "" — collapse to
    # None so the frontend can cleanly omit the section.
    description_html = (
        to_prolific_html(experiment_description) if experiment_description else ""
    ) or None
    return RaterStartResponse(
        rater_id=rater_id,
        session_start=session_start,
        session_end_time=build_session_end_time(session_start),
        experiment_name=experiment_name,
        experiment_description_html=description_html,
        human_prompt_prefix=human_prompt_prefix,
        human_prompt_suffix=human_prompt_suffix,
        completion_url=completion_url,
        rater_session_token=rater_session_token,
        assistance_method=assistance_method,
        assistance_instructions=assistance_instructions,
    )
