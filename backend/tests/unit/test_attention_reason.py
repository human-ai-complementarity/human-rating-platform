"""Unit tests for the experiment "needs attention" reason helper.

Pure-function tests; no DB or Prolific. The integration-side coverage (the
list endpoint actually surfacing the flag) lives in
tests/e2e/test_characterization.py.
"""

from __future__ import annotations

from models import ExperimentStatus, ProlificStudyStatus
from services.admin.status import compute_attention_reason

DRAFT = ExperimentStatus.DRAFT
LAUNCH = ExperimentStatus.LAUNCH
FINISHED = ExperimentStatus.FINISHED

UNPUBLISHED = ProlificStudyStatus.UNPUBLISHED
ACTIVE = ProlificStudyStatus.ACTIVE
AWAITING_REVIEW = ProlificStudyStatus.AWAITING_REVIEW
COMPLETED = ProlificStudyStatus.COMPLETED


def test_finished_experiment_is_never_actionable():
    # Even with a shortfall and a closed round, a terminal experiment has no action.
    assert (
        compute_attention_reason(
            status=FINISHED,
            remaining_actions=10,
            round_statuses=[COMPLETED],
        )
        is None
    )


def test_draft_with_no_rounds_is_not_actionable():
    assert compute_attention_reason(status=DRAFT, remaining_actions=0, round_statuses=[]) is None


def test_unpublished_round_draft_flags_publish_in_draft():
    reason = compute_attention_reason(
        status=DRAFT,
        remaining_actions=4,
        round_statuses=[UNPUBLISHED],
    )
    assert reason is not None
    assert "publish" in reason.lower()


def test_unpublished_round_draft_flags_publish_in_launch():
    # A published, closed round plus a fresh unpublished draft: publish wins.
    reason = compute_attention_reason(
        status=LAUNCH,
        remaining_actions=4,
        round_statuses=[COMPLETED, UNPUBLISHED],
    )
    assert reason is not None
    assert "publish" in reason.lower()


def test_active_round_still_collecting_is_not_actionable():
    # A round is live — the admin just waits, no dot.
    assert (
        compute_attention_reason(
            status=LAUNCH,
            remaining_actions=4,
            round_statuses=[ACTIVE],
        )
        is None
    )


def test_launch_all_rounds_closed_target_unmet_flags_new_round():
    reason = compute_attention_reason(
        status=LAUNCH,
        remaining_actions=4,
        round_statuses=[AWAITING_REVIEW],
    )
    assert reason is not None
    assert "launch another round" in reason.lower()


def test_launch_all_rounds_closed_target_met_flags_finish():
    reason = compute_attention_reason(
        status=LAUNCH,
        remaining_actions=0,
        round_statuses=[COMPLETED, AWAITING_REVIEW],
    )
    assert reason is not None
    assert "finish" in reason.lower()


def test_launch_with_one_open_round_is_not_actionable():
    # Mixed closed + active (no unpublished draft): still collecting → wait.
    assert (
        compute_attention_reason(
            status=LAUNCH,
            remaining_actions=4,
            round_statuses=[COMPLETED, ACTIVE],
        )
        is None
    )
