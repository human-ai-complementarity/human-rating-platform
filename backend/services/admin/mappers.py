from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from models import Experiment, Question, Rating, Rater, Upload
from schemas import ExperimentResponse
from .groups import GroupSnapshot

QUESTION_PREVIEW_LENGTH = 100


def isoformat_utc(value: datetime | None) -> str | None:
    """Serialize to RFC 3339 with a `Z` suffix instead of a `+00:00` offset.

    Analytics builds its JSON by hand rather than through a response model, and
    these columns are timezone-aware, so a bare `.isoformat()` emits `+00:00`.
    Clients expect the `Z` form used elsewhere on the wire.
    """
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_experiment_response(
    experiment: Experiment,
    *,
    question_count: int,
    rating_count: int,
    dataset_filenames: list[str] | None = None,
    attention_reason: str | None = None,
    spend_minor_units: int = 0,
    group: GroupSnapshot | None = None,
) -> ExperimentResponse:
    return ExperimentResponse(
        id=experiment.id,
        name=experiment.name,
        internal_name=experiment.internal_name,
        created_at=experiment.created_at,
        num_ratings_per_question=experiment.num_ratings_per_question,
        prolific_completion_url=experiment.prolific_completion_url,
        question_count=question_count,
        rating_count=rating_count,
        assistance_method=experiment.assistance_method,
        assistance_params=json.loads(experiment.assistance_params)
        if experiment.assistance_params
        else None,
        description=experiment.description,
        system_prompt=experiment.system_prompt,
        human_prompt_prefix=experiment.human_prompt_prefix,
        human_prompt_suffix=experiment.human_prompt_suffix,
        prolific_pool=experiment.prolific_pool,
        status=experiment.status,
        archived_at=experiment.archived_at,
        dataset_filenames=list(dataset_filenames or []),
        needs_attention=attention_reason is not None,
        attention_reason=attention_reason,
        spend_minor_units=spend_minor_units,
        group_id=group.group_id if group else None,
        group_name=group.group_name if group else None,
        dataset_id=group.dataset_id if group else None,
        dataset_name=group.dataset_name if group else None,
        wave=group.wave if group else None,
    )


def build_upload_response(upload: Upload) -> dict[str, Any]:
    return {
        "id": upload.id,
        "filename": upload.filename,
        "uploaded_at": upload.uploaded_at.isoformat(),
        "question_count": upload.question_count,
        "dataset_meta": json.loads(upload.dataset_meta) if upload.dataset_meta else None,
    }


def build_empty_analytics_payload(
    *,
    experiment_name: str,
    total_questions: int,
) -> dict[str, Any]:
    return {
        "experiment_name": experiment_name,
        "overview": {
            "total_ratings": 0,
            "total_questions": total_questions,
            "total_raters": 0,
            "avg_response_time_seconds": 0,
            "avg_confidence": 0,
        },
        "questions": [],
        "raters": [],
    }


def build_question_stats_bucket(question: Question) -> dict[str, Any]:
    return {
        "question_id": question.question_id,
        # Analytics is preview-oriented, so we intentionally cap the text length.
        "question_text": (
            question.question_text[:QUESTION_PREVIEW_LENGTH] + "..."
            if len(question.question_text) > QUESTION_PREVIEW_LENGTH
            else question.question_text
        ),
        "num_ratings": 0,
        "response_times": [],
        "confidences": [],
        "answers": [],
    }


def build_rater_stats_bucket(rater: Rater) -> dict[str, Any]:
    return {
        "prolific_id": rater.prolific_id,
        "study_id": rater.study_id,
        "session_start": isoformat_utc(rater.session_start),
        "session_end": isoformat_utc(rater.session_end),
        "is_active": rater.is_active,
        "num_ratings": 0,
        "response_times": [],
        "confidences": [],
    }


def build_question_analytics_item(stats: dict[str, Any]) -> dict[str, Any]:
    answers = stats["answers"]
    answer_counts: dict[str, int] = {}
    for answer in answers:
        answer_counts[str(answer)] = answer_counts.get(str(answer), 0) + 1

    response_times = stats["response_times"]
    confidences = stats["confidences"]

    return {
        "question_id": stats["question_id"],
        "question_text": stats["question_text"],
        "num_ratings": stats["num_ratings"],
        "avg_response_time_seconds": round(sum(response_times) / len(response_times), 2),
        "min_response_time_seconds": round(min(response_times), 2),
        "max_response_time_seconds": round(max(response_times), 2),
        "avg_confidence": round(sum(confidences) / len(confidences), 2),
        "answer_distribution": answer_counts,
    }


def build_rater_analytics_item(stats: dict[str, Any]) -> dict[str, Any]:
    response_times = stats["response_times"]
    confidences = stats["confidences"]
    total_time = sum(response_times)

    return {
        "prolific_id": stats["prolific_id"],
        "study_id": stats["study_id"],
        "session_start": stats["session_start"],
        "session_end": stats["session_end"],
        "is_active": stats["is_active"],
        "num_ratings": stats["num_ratings"],
        "total_response_time_seconds": round(total_time, 2),
        "avg_response_time_seconds": round(
            total_time / max(int(stats["num_ratings"]), 1),
            2,
        ),
        "avg_confidence": round(sum(confidences) / len(confidences), 2),
    }


def build_analytics_payload(
    *,
    experiment_name: str,
    total_questions: int,
    ratings: list[tuple[Rating, Question, Rater]],
) -> dict[str, Any]:
    response_times: list[float] = []
    confidences: list[int] = []
    question_stats: dict[str, dict[str, Any]] = {}
    rater_stats: dict[str, dict[str, Any]] = {}

    for rating, question, rater in ratings:
        response_time = (rating.time_submitted - rating.time_started).total_seconds()
        response_times.append(response_time)
        confidences.append(rating.confidence)

        q_id = question.question_id
        if q_id not in question_stats:
            question_stats[q_id] = build_question_stats_bucket(question)
        question_stats[q_id]["num_ratings"] += 1
        question_stats[q_id]["response_times"].append(response_time)
        question_stats[q_id]["confidences"].append(rating.confidence)
        question_stats[q_id]["answers"].append(rating.answer)

        # We group by prolific_id so one participant appears once even if they submit many rows.
        r_id = rater.prolific_id
        if r_id not in rater_stats:
            rater_stats[r_id] = build_rater_stats_bucket(rater)
        rater_stats[r_id]["num_ratings"] += 1
        rater_stats[r_id]["response_times"].append(response_time)
        rater_stats[r_id]["confidences"].append(rating.confidence)

    questions = [build_question_analytics_item(stats) for stats in question_stats.values()]
    raters = [build_rater_analytics_item(stats) for stats in rater_stats.values()]
    raters.sort(key=lambda item: item["num_ratings"], reverse=True)

    return {
        "experiment_name": experiment_name,
        "overview": {
            "total_ratings": len(ratings),
            "total_questions": total_questions,
            "total_raters": len(rater_stats),
            "avg_response_time_seconds": round(sum(response_times) / len(response_times), 2),
            "min_response_time_seconds": round(min(response_times), 2),
            "max_response_time_seconds": round(max(response_times), 2),
            "avg_confidence": round(sum(confidences) / len(confidences), 2),
        },
        "questions": questions,
        "raters": raters,
    }
