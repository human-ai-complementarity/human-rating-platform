from __future__ import annotations

from models import Question
from services.rater.selectors import (
    build_question_selection_groups,
    build_selected_question,
)


def _question(question_id: int, parent_question_id: int | None = None) -> Question:
    return Question(
        id=question_id,
        experiment_id=1,
        question_id=f"q{question_id}",
        question_text="text",
        parent_question_id=parent_question_id,
    )


def _select(open_questions=(), backfill_questions=(), full_questions=(), **kwargs):
    return build_selected_question(
        open_questions=list(open_questions),
        backfill_questions=list(backfill_questions),
        full_questions=list(full_questions),
        **kwargs,
    )


def test_groups_split_into_open_backfill_full():
    q_open = _question(1)  # 1 rating, 0 reserved: still an open slot
    q_backfill = _question(2)  # 1 rating + 1 reservation: covered but not committed
    q_saturated = _question(3)  # 1 rating + 3 reservations: backfill cap reached
    q_done = _question(4)  # committed target met
    q_new = _question(5)  # None counts treated as zero

    open_questions, backfill_questions, full_questions = build_question_selection_groups(
        eligible_questions=[
            (q_open, 1, 0),
            (q_backfill, 1, 1),
            (q_saturated, 1, 3),
            (q_done, 2, 0),
            (q_new, None, None),
        ],
        target_ratings_per_question=2,
    )

    assert [(q.id, c) for q, c in open_questions] == [(1, 1), (5, 0)]
    assert [(q.id, c) for q, c in backfill_questions] == [(2, 2)]
    assert [q.id for q in full_questions] == [3, 4]


def test_open_work_preferred_over_backfill():
    q_open = _question(1)
    q_backfill = _question(2)
    selected = _select(
        open_questions=[(q_open, 1)],
        backfill_questions=[(q_backfill, 2)],
    )
    assert selected is q_open


def test_backfill_served_when_no_open_work():
    q_backfill = _question(1)
    selected = _select(backfill_questions=[(q_backfill, 2)])
    assert selected is q_backfill


def test_full_not_served_to_real_raters():
    assert _select(full_questions=[_question(1)], allow_full=False) is None


def test_full_served_in_preview():
    question = _question(1)
    assert _select(full_questions=[question], allow_full=True) is question


def test_least_covered_question_preferred():
    q_more = _question(1)
    q_less = _question(2)
    selected = _select(open_questions=[(q_more, 2), (q_less, 0)])
    assert selected is q_less


def test_in_progress_group_open_sibling_preferred():
    sibling = _question(2, parent_question_id=10)
    unrelated = _question(3)
    selected = _select(
        open_questions=[(unrelated, 0), (sibling, 1)],
        in_progress_parent_ids={10},
    )
    assert selected is sibling


def test_in_progress_group_backfill_sibling_preferred_over_unrelated_open():
    # Sibling grouping outranks tier: a covered-but-uncommitted sibling keeps
    # the rater in their group rather than jumping to unrelated open work.
    sibling_backfill = _question(2, parent_question_id=10)
    unrelated = _question(3)
    selected = _select(
        open_questions=[(unrelated, 0)],
        backfill_questions=[(sibling_backfill, 2)],
        in_progress_parent_ids={10},
    )
    assert selected is sibling_backfill


def test_in_progress_group_full_sibling_skipped_for_real_raters():
    # A sibling whose committed target is met is not served just to finish
    # the group — the extra rating would be truncated in analysis.
    sibling_full = _question(2, parent_question_id=10)
    unrelated = _question(3)
    selected = _select(
        open_questions=[(unrelated, 0)],
        full_questions=[sibling_full],
        in_progress_parent_ids={10},
        allow_full=False,
    )
    assert selected is unrelated


def test_in_progress_group_full_sibling_served_in_preview():
    sibling_full = _question(2, parent_question_id=10)
    unrelated = _question(3)
    selected = _select(
        open_questions=[(unrelated, 0)],
        full_questions=[sibling_full],
        in_progress_parent_ids={10},
        allow_full=True,
    )
    assert selected is sibling_full


def test_nothing_left_returns_none():
    assert _select() is None
