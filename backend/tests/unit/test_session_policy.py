from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.session_policy import (
    DEFAULT_SESSION_DURATION_MINUTES,
    MAX_SESSION_DURATION_MINUTES,
    MIN_SESSION_DURATION_MINUTES,
    SessionPolicy,
    resolve_session_policy,
)


def test_defaults_reproduce_the_pre_configurable_clocks() -> None:
    """The numbers this replaced were 60 minutes, 3600 seconds and a 30-minute
    reservation window. Nothing may drift off those at the default."""
    policy = SessionPolicy()

    assert policy.duration_minutes == 60
    assert policy.duration_seconds == 3600
    assert policy.assignment_ttl_minutes == 30
    assert policy.grace_minutes == 0
    assert policy.token_ttl_seconds == 3600


def test_deadline_and_hard_deadline_coincide_without_grace() -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    policy = SessionPolicy()

    assert policy.deadline(start) == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    assert policy.hard_deadline(start) == policy.deadline(start)


def test_grace_extends_the_hard_deadline_and_the_token() -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    policy = SessionPolicy(duration_minutes=60, grace_minutes=5)

    assert policy.deadline(start) == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    assert policy.hard_deadline(start) == datetime(2026, 1, 1, 13, 5, tzinfo=UTC)
    # A token dying on the deadline would reject the submission grace exists for.
    assert policy.token_ttl_seconds == 65 * 60


@pytest.mark.parametrize(
    ("duration", "expected_ttl"),
    [
        (MIN_SESSION_DURATION_MINUTES, 2),
        (20, 10),
        (60, 30),
        (90, 45),
        (MAX_SESSION_DURATION_MINUTES, 60),
    ],
)
def test_reservation_window_tracks_the_session(duration: int, expected_ttl: int) -> None:
    """A fixed 30 minutes would outlive a short session and expire three times
    inside a long one."""
    assert SessionPolicy(duration_minutes=duration).assignment_ttl_minutes == expected_ttl


def test_resolve_returns_the_default_for_any_experiment() -> None:
    class _Experiment:
        id = 1

    assert resolve_session_policy(_Experiment()).duration_minutes == (
        DEFAULT_SESSION_DURATION_MINUTES
    )


@pytest.mark.parametrize(
    "duration", [0, -1, MIN_SESSION_DURATION_MINUTES - 1, MAX_SESSION_DURATION_MINUTES + 1]
)
def test_a_policy_outside_the_bounds_cannot_be_constructed(duration: int) -> None:
    """The bounds are enforced on the object, not only at the API boundary: a
    policy is also built from stored values, and duration_seconds == 0 would
    divide by zero in calculate_recommendation."""
    with pytest.raises(ValueError):
        SessionPolicy(duration_minutes=duration)


def test_negative_grace_is_rejected() -> None:
    with pytest.raises(ValueError):
        SessionPolicy(grace_minutes=-1)
