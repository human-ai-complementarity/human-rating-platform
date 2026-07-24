"""Database models using SQLModel.

These models are the source of truth for the schema. Database migrations
are generated from these definitions using `alembic revision --autogenerate`,
then reviewed and committed. See README Migrations section for workflow.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel

SESSION_DURATION_MINUTES = 60  # Hard-coded 1 hour per rater


class ProlificStudyStatus(str, Enum):
    """Prolific study lifecycle states."""

    UNPUBLISHED = "UNPUBLISHED"
    PUBLISHING = "PUBLISHING"
    ACTIVE = "ACTIVE"
    SCHEDULED = "SCHEDULED"
    PAUSED = "PAUSED"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    COMPLETED = "COMPLETED"

    @classmethod
    def _missing_(cls, value: object) -> "ProlificStudyStatus | None":
        # Prolific's API returns some statuses space-separated (e.g.
        # "AWAITING REVIEW") while our members use underscores. Normalize
        # before giving up so a STOP transition or status refresh doesn't
        # raise ValueError on the space form. Unknown values still fall
        # through to the standard ValueError.
        if isinstance(value, str):
            normalized = value.strip().upper().replace(" ", "_")
            for member in cls:
                if member.value == normalized:
                    return member
        return None


ROUND_TERMINAL_STATUSES = frozenset(
    {ProlificStudyStatus.AWAITING_REVIEW, ProlificStudyStatus.COMPLETED}
)


class ExperimentStatus(str, Enum):
    """Experiment lifecycle states.

    DRAFT     — freshly created or piloting; all config editable.
    LAUNCH    — first main round created; experiment-level config is frozen.
    FINISHED  — terminal (no more rounds); becomes selectable as an exclusion
                source for other experiments.
    """

    DRAFT = "DRAFT"
    LAUNCH = "LAUNCH"
    FINISHED = "FINISHED"


class StepType(str, Enum):
    """Assistance interaction step types."""

    NONE = "none"  # method produced no assistance (terminal)
    DISPLAY = "display"  # show static content to the rater (terminal)
    ASK_INPUT = "ask_input"  # ask the rater a sub-question, then call advance()
    COMPLETE = "complete"  # multi-turn interaction finished, show final result (terminal)
    SKIP = "skip"  # unrecoverable error mid-session; question skipped for retry later (terminal)


class Experiment(SQLModel, table=True):
    __tablename__ = "experiments"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(sa_column=Column(String(255), nullable=False))
    internal_name: Optional[str] = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    num_ratings_per_question: int = Field(
        default=3,
        sa_column=Column(Integer, nullable=False, server_default=text("3")),
    )
    prolific_completion_url: Optional[str] = Field(
        default=None,
        sa_column=Column(String(2048), nullable=True),
    )
    assistance_method: str = Field(
        default="none",
        sa_column=Column(String(64), nullable=False, server_default=text("'none'")),
    )
    assistance_params: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )  # JSON-encoded method-specific parameters
    description: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    system_prompt: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    # Per-question framing. `human_prompt_prefix` is rendered above the question text,
    # `human_prompt_suffix` below it. Either or both may be set; the AI's analogue is
    # the system message append handled via `system_prompt`.
    human_prompt_prefix: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    human_prompt_suffix: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    prolific_pool: Optional[str] = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )
    # Prolific participant group ID that collects everyone who joins this
    # experiment. Populated lazily on first rater entry. Used as the source
    # for `excluded_experiment_ids` blocklists on later experiments.
    prolific_participant_group_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )
    status: ExperimentStatus = Field(
        default=ExperimentStatus.DRAFT,
        sa_column=Column(
            String(16),
            nullable=False,
            server_default=text("'DRAFT'"),
        ),
    )


class Question(SQLModel, table=True):
    __tablename__ = "questions"

    id: Optional[int] = Field(default=None, primary_key=True)
    experiment_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    question_id: str = Field(sa_column=Column(String(255), nullable=False))
    question_text: str = Field(sa_column=Column(Text, nullable=False))
    gt_answer: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    options: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    question_type: str = Field(
        default="MC",
        sa_column=Column(String(16), nullable=False, server_default=text("'MC'")),
    )
    extra_data: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    parent_question_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("questions.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )


class Rater(SQLModel, table=True):
    __tablename__ = "raters"
    __table_args__ = (
        UniqueConstraint(
            "prolific_id",
            "experiment_id",
            name="uq_rater_prolific_experiment",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    prolific_id: str = Field(sa_column=Column(String(64), nullable=False))
    study_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )  # Prolific STUDY_ID
    session_id: Optional[str] = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )  # Prolific SESSION_ID
    experiment_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    session_start: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    session_end: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
    )
    is_preview: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )


class Rating(SQLModel, table=True):
    __tablename__ = "ratings"
    __table_args__ = (
        UniqueConstraint(
            "question_id",
            "rater_id",
            name="uq_rating_question_rater",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    question_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("questions.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    rater_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("raters.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    assistance_session_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("assistance_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    answer: str = Field(sa_column=Column(Text, nullable=False))
    confidence: int = Field(sa_column=Column(Integer, nullable=False))
    time_started: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    time_submitted: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )


class ExperimentRound(SQLModel, table=True):
    """Tracks each Prolific study launched for an experiment."""

    __tablename__ = "experiment_rounds"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "round_number",
            name="uq_experiment_round_number",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    experiment_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    round_number: int = Field(
        sa_column=Column(Integer, nullable=False)
    )  # 0 = pilot, 1+ = main rounds
    prolific_study_id: str = Field(sa_column=Column(String(128), nullable=False))
    prolific_study_status: ProlificStudyStatus = Field(sa_column=Column(String(32), nullable=False))
    description: str = Field(sa_column=Column(Text, nullable=False))
    estimated_completion_time: int = Field(sa_column=Column(Integer, nullable=False))
    reward: int = Field(sa_column=Column(Integer, nullable=False))
    device_compatibility: str = Field(
        sa_column=Column(String(256), nullable=False)
    )  # JSON-encoded list, e.g. '["desktop"]'
    study_label: Optional[str] = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )
    screeners: Optional[str] = Field(
        default=None,
        sa_column=Column(String(256), nullable=True),
    )  # JSON-encoded list, e.g. '["ai_taskers", "fact_checkers"]'
    excluded_experiment_ids: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )  # JSON-encoded list of experiment IDs whose participant groups block this round
    places_requested: int = Field(sa_column=Column(Integer, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )


class Upload(SQLModel, table=True):
    __tablename__ = "uploads"

    id: Optional[int] = Field(default=None, primary_key=True)
    experiment_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    filename: str = Field(sa_column=Column(String(512), nullable=False))
    uploaded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    question_count: int = Field(sa_column=Column(Integer, nullable=False))
    dataset_meta: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )  # JSON of the dataset metadata parsed from this upload, if any


class AssistanceSession(SQLModel, table=True):
    """Tracks the state of a multi-turn assistance interaction for a rater/question pair."""

    __tablename__ = "assistance_sessions"
    __table_args__ = (
        UniqueConstraint(
            "rater_id",
            "question_id",
            name="uq_assistance_session_rater_question",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    rater_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("raters.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    experiment_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    question_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("questions.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    method_name: str = Field(sa_column=Column(String(64), nullable=False))
    params: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )  # JSON-encoded snapshot of experiment.assistance_params at session creation
    step_type: str = Field(sa_column=Column(String(32), nullable=False))
    state: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )  # JSON-encoded backend-only state passed to advance() between turns
    payload: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )  # JSON-encoded last payload sent to frontend (used to restore UI on resume)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    is_complete: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
