from __future__ import annotations

import random

from models import Question


def build_question_selection_groups(
    *,
    eligible_questions: list[tuple[Question, int | None]],
    target_ratings_per_question: int,
) -> tuple[list[tuple[Question, int]], list[Question]]:
    under_quota: list[tuple[Question, int]] = []
    at_quota: list[Question] = []

    for question, count in eligible_questions:
        rating_count = int(count or 0)
        if rating_count < target_ratings_per_question:
            under_quota.append((question, rating_count))
        else:
            at_quota.append(question)

    return under_quota, at_quota


def build_selected_question(
    *,
    under_quota: list[tuple[Question, int]],
    at_quota: list[Question],
    in_progress_parent_ids: set[int] | None = None,
    allow_at_quota: bool = False,
) -> Question | None:
    """Pick the next question, or None when no needed work remains.

    Questions at quota are never served (`allow_at_quota=False`) so a rater
    stops as soon as every question has its target ratings — extra ratings
    would be truncated in analysis, so collecting them wastes rater time.
    Preview sessions pass `allow_at_quota=True`: they produce no real data
    and should always be able to walk the full flow.
    """
    in_progress_parent_ids = in_progress_parent_ids or set()

    # If the rater has started a parent group, keep them in it until all its
    # remaining children are rated — sibling sub-questions should be served
    # consecutively rather than randomly interleaved with unrelated questions.
    if in_progress_parent_ids:
        in_group_under = [
            (q, c) for q, c in under_quota if q.parent_question_id in in_progress_parent_ids
        ]
        if in_group_under:
            return _pick_least_rated(in_group_under)

        if allow_at_quota:
            in_group_at = [q for q in at_quota if q.parent_question_id in in_progress_parent_ids]
            if in_group_at:
                return random.choice(in_group_at)

    # Otherwise prioritize the least-rated questions first to keep experiment
    # coverage balanced.
    if under_quota:
        return _pick_least_rated(under_quota)

    if allow_at_quota and at_quota:
        return random.choice(at_quota)

    return None


def _pick_least_rated(candidates: list[tuple[Question, int]]) -> Question:
    candidates.sort(key=lambda item: item[1])
    min_count = candidates[0][1]
    return random.choice([q for q, count in candidates if count == min_count])
