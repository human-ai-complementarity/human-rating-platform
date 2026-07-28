from __future__ import annotations

from models import Question
from services.rater.selectors import build_question_selection_groups, build_selected_question


def _question(question_id: int, parent_question_id: int | None = None) -> Question:
    return Question(
        id=question_id,
        experiment_id=1,
        question_id=f"q{question_id}",
        question_text="text",
        parent_question_id=parent_question_id,
    )


def test_groups_split_on_target():
    q_under = _question(1)
    q_at = _question(2)
    under, at = build_question_selection_groups(
        eligible_questions=[(q_under, 1), (q_at, 2), (_question(3), None)],
        target_ratings_per_question=2,
    )
    assert (q_under, 1) in under
    assert at == [q_at]
    # None count is treated as zero, i.e. under quota.
    assert any(q.id == 3 and c == 0 for q, c in under)


def test_at_quota_not_served_to_real_raters():
    assert (
        build_selected_question(under_quota=[], at_quota=[_question(1)], allow_at_quota=False)
        is None
    )


def test_at_quota_served_when_allowed():
    question = _question(1)
    selected = build_selected_question(under_quota=[], at_quota=[question], allow_at_quota=True)
    assert selected is question


def test_least_rated_question_preferred():
    q_more = _question(1)
    q_less = _question(2)
    selected = build_selected_question(
        under_quota=[(q_more, 2), (q_less, 0)],
        at_quota=[],
    )
    assert selected is q_less


def test_in_progress_group_under_quota_sibling_preferred():
    sibling = _question(2, parent_question_id=10)
    unrelated = _question(3)
    selected = build_selected_question(
        under_quota=[(unrelated, 0), (sibling, 1)],
        at_quota=[],
        in_progress_parent_ids={10},
    )
    assert selected is sibling


def test_in_progress_group_at_quota_sibling_skipped_for_real_raters():
    # A sibling that already hit its target is not served just to finish the
    # group — the extra rating would be truncated in analysis.
    sibling_at_quota = _question(2, parent_question_id=10)
    unrelated = _question(3)
    selected = build_selected_question(
        under_quota=[(unrelated, 0)],
        at_quota=[sibling_at_quota],
        in_progress_parent_ids={10},
        allow_at_quota=False,
    )
    assert selected is unrelated


def test_in_progress_group_at_quota_sibling_served_in_preview():
    sibling_at_quota = _question(2, parent_question_id=10)
    unrelated = _question(3)
    selected = build_selected_question(
        under_quota=[(unrelated, 0)],
        at_quota=[sibling_at_quota],
        in_progress_parent_ids={10},
        allow_at_quota=True,
    )
    assert selected is sibling_at_quota


def test_nothing_left_returns_none():
    assert build_selected_question(under_quota=[], at_quota=[]) is None
