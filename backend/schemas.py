from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models import ExperimentStatus, ProlificStudyStatus, StepType


# Allowed values for Prolific's `study_labels` field. The Prolific API also
# accepts AI-task variants, but we expose only the data-collection set our
# researchers actually pick from.
StudyLabel = Literal[
    "annotation",
    "survey",
    "decision_making_task",
    "writing_task",
    "interview",
    "other",
]


# Pre-screener filter keys we apply to Prolific studies. Concrete filter IDs
# and thresholds live in services.admin.prolific.SCREENER_FILTERS.
Screener = Literal["ai_taskers", "fact_checkers", "approval_rate"]


# Prolific schemas
class ProlificStudyConfig(BaseModel):
    description: str
    estimated_completion_time: int = Field(ge=1)
    reward: int = Field(ge=1)
    total_available_places: int = Field(ge=1)
    device_compatibility: list[Literal["desktop", "tablet", "mobile"]] = Field(
        default_factory=lambda: ["desktop"]
    )


def _dedupe_preserve_order(ids: list[int]) -> list[int]:
    return list(dict.fromkeys(ids))


class PilotStudyCreate(BaseModel):
    description: str
    estimated_completion_time: int = Field(ge=1)
    reward: int = Field(ge=1)
    pilot_places: int = Field(default=5, ge=1)
    device_compatibility: list[Literal["desktop", "tablet", "mobile"]] = Field(
        default_factory=lambda: ["desktop"]
    )
    study_label: StudyLabel = "annotation"
    screeners: list[Screener] = Field(
        default_factory=lambda: ["ai_taskers", "fact_checkers", "approval_rate"]
    )
    excluded_experiment_ids: list[int] = Field(default_factory=list)

    @field_validator("excluded_experiment_ids")
    @classmethod
    def _dedupe(cls, v: list[int]) -> list[int]:
        return _dedupe_preserve_order(v)


class ExperimentRoundCreate(BaseModel):
    places: int = Field(ge=1)


class ExperimentRoundUpdate(BaseModel):
    description: Optional[str] = None
    estimated_completion_time: Optional[int] = Field(default=None, ge=1)
    reward: Optional[int] = Field(default=None, ge=1)
    places: Optional[int] = Field(default=None, ge=1)
    device_compatibility: Optional[list[Literal["desktop", "tablet", "mobile"]]] = None
    study_label: Optional[StudyLabel] = None
    screeners: Optional[list[Screener]] = None
    excluded_experiment_ids: Optional[list[int]] = None

    @field_validator("excluded_experiment_ids")
    @classmethod
    def _dedupe(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        return None if v is None else _dedupe_preserve_order(v)

    def has_any(self) -> bool:
        return any(
            getattr(self, field) is not None
            for field in (
                "description",
                "estimated_completion_time",
                "reward",
                "places",
                "device_compatibility",
                "study_label",
                "screeners",
                "excluded_experiment_ids",
            )
        )


class RecommendationResponse(BaseModel):
    avg_time_per_question_seconds: float
    remaining_rating_actions: int
    total_hours_remaining: float
    recommended_places: int
    is_complete: bool


class ExperimentRoundResponse(BaseModel):
    id: int
    round_number: int
    prolific_study_id: str
    prolific_study_status: ProlificStudyStatus
    places_requested: int
    description: str
    estimated_completion_time: int
    reward: int
    device_compatibility: list[str]
    study_label: Optional[str] = None
    screeners: list[Screener] = Field(default_factory=list)
    excluded_experiment_ids: list[int] = Field(default_factory=list)
    created_at: datetime
    prolific_study_url: str

    model_config = ConfigDict(from_attributes=True)


class PlatformStatus(BaseModel):
    prolific_enabled: bool
    currency_code: str | None = None
    currency_symbol: str | None = None


# Experiment schemas
class ExperimentCreate(BaseModel):
    name: str
    # `internal_name` is capped to match the DB column (`String(255)`) so an
    # overlong value is rejected by Pydantic as a clean 422 instead of falling
    # through to Postgres and surfacing as a 500.
    internal_name: Optional[str] = Field(default=None, max_length=255)
    num_ratings_per_question: int = 3
    prolific_completion_url: Optional[str] = None
    prolific: Optional[ProlificStudyConfig] = None
    assistance_method: str = "none"
    assistance_params: Optional[dict] = None


class ExperimentResponse(BaseModel):
    id: int
    name: str
    internal_name: Optional[str] = None
    created_at: datetime
    num_ratings_per_question: int
    prolific_completion_url: Optional[str] = None
    question_count: int = 0
    rating_count: int = 0
    assistance_method: str = "none"
    assistance_params: Optional[dict] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    human_prompt_prefix: Optional[str] = None
    human_prompt_suffix: Optional[str] = None
    prolific_pool: Optional[str] = Field(default=None, max_length=255)
    status: ExperimentStatus = ExperimentStatus.DRAFT
    # Non-null when the experiment has been archived (soft-hidden from the
    # default admin list). NULL/absent means active.
    archived_at: Optional[datetime] = None
    # Filenames of every Upload attached to this experiment. Used client-side
    # so admins can find an experiment by its dataset filename when picking
    # experiments to exclude from a new round.
    dataset_filenames: list[str] = Field(default_factory=list)
    # Set when the experiment has a pending admin action (e.g. rounds closed
    # but target unmet, or an unpublished round draft). `attention_reason` is a
    # short human sentence; the list view shows a dot with it as the tooltip.
    # Only computed by the list endpoint — single-experiment reads leave it off.
    needs_attention: bool = False
    attention_reason: Optional[str] = None
    # Total Prolific spend across this experiment's rounds, in the workspace
    # currency's minor units (sum of each round's Prolific `total_cost`).
    # 0 when no round has been synced yet. List endpoint only.
    spend_minor_units: int = 0

    model_config = ConfigDict(from_attributes=True)


class ExperimentUpdate(BaseModel):
    assistance_method: str
    assistance_params: Optional[dict] = None
    # `name` is the public, rater-facing name. None means "leave unchanged"; an
    # empty/whitespace value is rejected in update_experiment (the public name is
    # required). Capped to the DB column width so an overlong value 422s cleanly.
    name: Optional[str] = Field(default=None, max_length=255)
    internal_name: Optional[str] = Field(default=None, max_length=255)
    # Dataset metadata fields — each is sent only when the admin edits it.
    # An explicit "" clears the value; None means "leave unchanged".
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    human_prompt_prefix: Optional[str] = None
    human_prompt_suffix: Optional[str] = None
    prolific_pool: Optional[str] = Field(default=None, max_length=255)


# Question schemas
class QuestionResponse(BaseModel):
    id: int
    question_id: str
    question_text: str
    options: Optional[str] = None
    question_type: str
    parent_question_text: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# Rater schemas
class RaterStartResponse(BaseModel):
    rater_id: int
    session_start: datetime
    session_end_time: datetime
    experiment_name: str
    # Pre-rendered HTML for the rater intro screen (markdown converted via
    # `to_prolific_html` so the splash matches what Prolific shows externally).
    # None when no description is set; "" never appears.
    experiment_description_html: Optional[str] = None
    # Per-question framing. `human_prompt_prefix` is rendered above each question,
    # `human_prompt_suffix` below it. Constant for the session, sent once at start.
    human_prompt_prefix: Optional[str] = None
    human_prompt_suffix: Optional[str] = None
    completion_url: Optional[str] = None
    rater_session_token: str
    assistance_method: str = "none"
    assistance_instructions: Optional[str] = None


class SessionStatusResponse(BaseModel):
    is_active: bool
    time_remaining_seconds: int
    questions_completed: int


# Rating schemas
class RatingSubmit(BaseModel):
    question_id: int
    answer: str
    confidence: int = Field(ge=1, le=5)
    time_started: datetime
    assistance_session_id: Optional[int] = None


class RatingResponse(BaseModel):
    id: int
    success: bool


# Assistance schemas
class AssistanceStartRequest(BaseModel):
    question_id: int


class AssistanceAdvanceRequest(BaseModel):
    session_id: int
    human_input: str


class AssistanceStepResponse(BaseModel):
    session_id: int
    type: StepType
    payload: dict
    is_terminal: bool
