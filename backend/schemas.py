from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from models import ProlificStudyStatus, StepType


# Prolific schemas
class ProlificStudyConfig(BaseModel):
    description: str
    estimated_completion_time: int = Field(ge=1)
    reward: int = Field(ge=1)
    total_available_places: int = Field(ge=1)
    device_compatibility: list[Literal["desktop", "tablet", "mobile"]] = Field(
        default_factory=lambda: ["desktop"]
    )


class PilotStudyCreate(BaseModel):
    description: str
    estimated_completion_time: int = Field(ge=1)
    reward: int = Field(ge=1)
    pilot_places: int = Field(default=5, ge=1)
    device_compatibility: list[Literal["desktop", "tablet", "mobile"]] = Field(
        default_factory=lambda: ["desktop"]
    )


class ExperimentRoundCreate(BaseModel):
    places: int = Field(ge=1)


class ExperimentRoundUpdate(BaseModel):
    description: Optional[str] = None
    estimated_completion_time: Optional[int] = Field(default=None, ge=1)
    reward: Optional[int] = Field(default=None, ge=1)
    places: Optional[int] = Field(default=None, ge=1)
    device_compatibility: Optional[list[Literal["desktop", "tablet", "mobile"]]] = None

    def has_any(self) -> bool:
        return any(
            getattr(self, field) is not None
            for field in (
                "description",
                "estimated_completion_time",
                "reward",
                "places",
                "device_compatibility",
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
    num_ratings_per_question: int = 3
    prolific_completion_url: Optional[str] = None
    prolific: Optional[ProlificStudyConfig] = None
    assistance_method: str = "none"
    assistance_params: Optional[dict] = None


class ExperimentResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    num_ratings_per_question: int
    prolific_completion_url: Optional[str] = None
    question_count: int = 0
    rating_count: int = 0
    assistance_method: str = "none"

    model_config = ConfigDict(from_attributes=True)


class ExperimentUpdate(BaseModel):
    assistance_method: str
    assistance_params: Optional[dict] = None


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
    completion_url: Optional[str] = None
    rater_session_token: str
    assistance_method: str = "none"


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
