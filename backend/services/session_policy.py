"""The clocks a rater session runs on, all derived from one number.

Before issue #102 the "one hour a rater gets" was written out in four
independent places — the deadline, the round-sizing maths, the session token's
TTL and the per-question reservation window — so changing it meant finding all
four and keeping them consistent by hand. A session length that is configurable
per experiment makes that untenable: get it wrong and the server serves
questions for three hours while the token dies after one, which only reproduces
on a non-default value and so passes CI.

Everything that needs to know how long a session lasts asks for a
`SessionPolicy` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import cycle at runtime: models will import this module
    from models import Experiment

# What an experiment gets unless it says otherwise. Stays at 60 so experiments
# created before the value was configurable behave exactly as they always have.
DEFAULT_SESSION_DURATION_MINUTES = 60

# Bounds for an admin-supplied session length. The floor keeps a typo from
# creating a session nobody can finish a single question in. The ceiling is a
# task-design judgement rather than a technical limit: two hours, on the view
# that anything needing longer should be split across more, shorter sessions
# rather than handed a longer clock — see issue #102 decision 2.
MIN_SESSION_DURATION_MINUTES = 5
MAX_SESSION_DURATION_MINUTES = 120

# Minutes past the deadline in which the question already on screen may still
# be submitted. Zero reproduces the pre-#102 behaviour, where the answer being
# typed when the clock ran out was discarded.
DEFAULT_GRACE_MINUTES = 0


@dataclass(frozen=True)
class SessionPolicy:
    duration_minutes: int = DEFAULT_SESSION_DURATION_MINUTES
    grace_minutes: int = DEFAULT_GRACE_MINUTES

    def __post_init__(self) -> None:
        # Enforced here rather than only at the API boundary because a policy
        # is constructed from stored values too, and duration_seconds == 0
        # would divide by zero in calculate_recommendation.
        if (
            not MIN_SESSION_DURATION_MINUTES
            <= self.duration_minutes
            <= MAX_SESSION_DURATION_MINUTES
        ):
            raise ValueError(
                f"session duration must be between {MIN_SESSION_DURATION_MINUTES} and "
                f"{MAX_SESSION_DURATION_MINUTES} minutes, got {self.duration_minutes}"
            )
        if self.grace_minutes < 0:
            raise ValueError(f"grace must not be negative, got {self.grace_minutes}")

    @property
    def duration_seconds(self) -> int:
        return self.duration_minutes * 60

    @property
    def assignment_ttl_minutes(self) -> int:
        """How long a served-but-unanswered question reserves its rating slot.

        Half the session: long enough to survive a refresh and a slow read,
        short enough that an abandoned reservation frees up within the session
        that created it. At the default hour this is 30 minutes, which is what
        the fixed constant it replaces was set to.
        """
        return max(1, self.duration_minutes // 2)

    @property
    def token_ttl_seconds(self) -> int:
        """How long a freshly issued session token stays valid.

        Covers the grace window as well as the session itself, because a token
        that dies on the deadline would reject the very submission the grace
        period exists to accept.
        """
        return (self.duration_minutes + self.grace_minutes) * 60

    def deadline(self, session_start: datetime) -> datetime:
        """When the rater stops being served new questions."""
        return session_start + timedelta(minutes=self.duration_minutes)

    def hard_deadline(self, session_start: datetime) -> datetime:
        """When the rater stops being able to submit anything at all."""
        return self.deadline(session_start) + timedelta(minutes=self.grace_minutes)


DEFAULT_SESSION_POLICY = SessionPolicy()


def resolve_session_policy(experiment: "Experiment") -> SessionPolicy:
    """The policy governing sessions for one experiment.

    The experiment is the seam: today every experiment gets the same defaults,
    and issue #102 decision 1 settles where a per-experiment override is
    stored. Taking it now means the call sites do not move when the value
    becomes per-experiment.
    """
    return DEFAULT_SESSION_POLICY
