from __future__ import annotations

import random

from models import Question


def build_question_selection_groups(
    *,
    eligible_questions: list[tuple[Question, int | None, int | None]],
    target_ratings_per_question: int,
) -> tuple[list[tuple[Question, int]], list[tuple[Question, int]], list[Question]]:
    """Split eligible questions into (open, backfill, done) tiers.

    open      — coverage (committed ratings + live reservations) below target:
                real work, served first.
    backfill  — committed ratings below target but every remaining slot is
                reserved: served next as abandonment insurance, since a
                reserving rater may never submit.
    done      — committed ratings at target: served last. An in-session
                rater's reward is already committed, so extra ratings cost
                no additional rater spend; they're flagged in the export and
                truncated in analysis, and can substitute when a rating is
                later quality-filtered out. Not entirely free on assisted
                experiments: each served question still pays the assistance
                method's LLM inference (multi-turn methods especially). If
                that spend becomes material, gate this tier on
                assistance_method == "none".

    Tier entries carry coverage so selection can prefer the least-covered
    question; each serve adds a reservation, which spreads concurrent raters
    across the tier instead of piling them onto one question.
    """
    open_questions: list[tuple[Question, int]] = []
    backfill_questions: list[tuple[Question, int]] = []
    done_questions: list[Question] = []

    for question, committed, reserved in eligible_questions:
        committed_count = int(committed or 0)
        coverage = committed_count + int(reserved or 0)
        if committed_count >= target_ratings_per_question:
            done_questions.append(question)
        elif coverage < target_ratings_per_question:
            open_questions.append((question, coverage))
        else:
            backfill_questions.append((question, coverage))

    return open_questions, backfill_questions, done_questions


def build_selected_question(
    *,
    open_questions: list[tuple[Question, int]],
    backfill_questions: list[tuple[Question, int]],
    done_questions: list[Question],
    in_progress_parent_ids: set[int] | None = None,
) -> Question | None:
    """Pick the next question, or None once the rater has rated everything.

    Raters are kept busy for their whole session: open slots first, then
    in-flight questions that still lack committed ratings, then done
    questions whose extras cost no rater spend. Recruiting cost is controlled
    elsewhere (the study auto-stops at the committed target); serving is
    only about getting the most out of raters already paid for.
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

        in_group_done = [
            q for q in done_questions if q.parent_question_id in in_progress_parent_ids
        ]
        if in_group_done:
            return random.choice(in_group_done)

    # Otherwise prioritize the least-covered questions first to keep
    # experiment coverage balanced.
    if open_questions:
        return _pick_least_covered(open_questions)

    if backfill_questions:
        return _pick_least_covered(backfill_questions)

    if done_questions:
        return random.choice(done_questions)

    return None


def _pick_least_covered(candidates: list[tuple[Question, int]]) -> Question:
    candidates.sort(key=lambda item: item[1])
    min_count = candidates[0][1]
    return random.choice([q for q, count in candidates if count == min_count])
