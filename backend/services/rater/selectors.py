from __future__ import annotations

import random

from models import Question

# Extra raters allowed to work a question in parallel beyond its remaining
# committed deficit. Insurance against a reserving rater abandoning: an
# in-session rater's redundant rating costs nothing (their reward is already
# committed, extras are truncated in analysis), while a round closing one
# rating short forces a whole extra paid round. The cap just keeps a crowd
# from queueing onto the same last question.
BACKFILL_EXTRA_RESERVATIONS = 2


def build_question_selection_groups(
    *,
    eligible_questions: list[tuple[Question, int | None, int | None]],
    target_ratings_per_question: int,
) -> tuple[list[tuple[Question, int]], list[tuple[Question, int]], list[Question]]:
    """Split eligible questions into (open, backfill, full) tiers.

    open      — coverage (committed ratings + live reservations) below target:
                real work, served first.
    backfill  — committed ratings below target but every remaining slot is
                reserved: servable as abandonment insurance while fewer than
                BACKFILL_EXTRA_RESERVATIONS extra raters hold it.
    full      — committed ratings at target, or backfill already saturated:
                never served to real raters; preview sessions may still see
                these so admins can always walk the flow.

    Tier entries carry coverage so selection can prefer the least-covered
    question; each serve adds a reservation, which spreads concurrent raters
    across the tier instead of piling them onto one question.
    """
    open_questions: list[tuple[Question, int]] = []
    backfill_questions: list[tuple[Question, int]] = []
    full_questions: list[Question] = []

    for question, committed, reserved in eligible_questions:
        committed_count = int(committed or 0)
        coverage = committed_count + int(reserved or 0)
        if (
            committed_count >= target_ratings_per_question
            or coverage >= target_ratings_per_question + BACKFILL_EXTRA_RESERVATIONS
        ):
            full_questions.append(question)
        elif coverage < target_ratings_per_question:
            open_questions.append((question, coverage))
        else:
            backfill_questions.append((question, coverage))

    return open_questions, backfill_questions, full_questions


def build_selected_question(
    *,
    open_questions: list[tuple[Question, int]],
    backfill_questions: list[tuple[Question, int]],
    full_questions: list[Question],
    in_progress_parent_ids: set[int] | None = None,
    allow_full: bool = False,
) -> Question | None:
    """Pick the next question, or None when no servable work remains.

    Real raters (`allow_full=False`) get open work first, then backfill;
    they stop as soon as every question either has its committed target or
    is saturated with in-flight raters. Preview sessions pass
    `allow_full=True`: they produce no real data and should always be able
    to walk the full flow.
    """
    in_progress_parent_ids = in_progress_parent_ids or set()

    # If the rater has started a parent group, keep them in it until all its
    # remaining children are rated — sibling sub-questions should be served
    # consecutively rather than randomly interleaved with unrelated questions.
    if in_progress_parent_ids:
        for tier in (open_questions, backfill_questions):
            in_group = [(q, c) for q, c in tier if q.parent_question_id in in_progress_parent_ids]
            if in_group:
                return _pick_least_covered(in_group)

        if allow_full:
            in_group_full = [
                q for q in full_questions if q.parent_question_id in in_progress_parent_ids
            ]
            if in_group_full:
                return random.choice(in_group_full)

    # Otherwise prioritize the least-covered questions first to keep
    # experiment coverage balanced.
    if open_questions:
        return _pick_least_covered(open_questions)

    if backfill_questions:
        return _pick_least_covered(backfill_questions)

    if allow_full and full_questions:
        return random.choice(full_questions)

    return None


def _pick_least_covered(candidates: list[tuple[Question, int]]) -> Question:
    candidates.sort(key=lambda item: item[1])
    min_count = candidates[0][1]
    return random.choice([q for q, count in candidates if count == min_count])
