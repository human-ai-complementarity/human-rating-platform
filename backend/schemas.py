from datetime import datetime
from typing import Annotated, Literal, Optional

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
    # Prolific's own `total_cost` for this round's study (rewards + fee + VAT),
    # in minor units. Null until the round has been synced from Prolific.
    total_cost: Optional[int] = None
    # Raters who submitted the study, and raters holding a place and still
    # working. Null until the round has been synced from Prolific. Places still
    # open = places_requested minus both.
    submissions_completed: Optional[int] = None
    submissions_in_progress: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class ProlificPricingResponse(BaseModel):
    """Prolific's fee rates, as fractions (0.2 = 20%).

    Lets the round form estimate what Prolific will charge (rewards + fee + VAT
    on the fee) before a study exists to read `total_cost` from. Null when the
    integration is disabled or the rates could not be fetched.
    """

    fees_percentage: float
    vat_percentage: float
    fees_per_submission: float


class PlatformStatus(BaseModel):
    prolific_enabled: bool
    currency_code: str | None = None
    currency_symbol: str | None = None
    pricing: ProlificPricingResponse | None = None


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
    # Optional during backfill; required-for-launch is a follow-up.
    group_id: Optional[int] = None


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
    # Computed by both the list and single-experiment (get_experiment) reads
    # via the shared _build_experiment_responses helper.
    needs_attention: bool = False
    attention_reason: Optional[str] = None
    # Total Prolific spend across this experiment's rounds, in the workspace
    # currency's minor units (sum of each round's Prolific `total_cost`).
    # 0 when no round has been synced yet. Populated by both the list and
    # single-experiment reads via the shared enrichment helper.
    spend_minor_units: int = 0
    # Inherited from the experiment group when one is attached; all None
    # when the experiment is ungrouped.
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    dataset_id: Optional[int] = None
    dataset_name: Optional[str] = None
    wave: Optional[str] = None

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
    # Omitted = leave unchanged; explicit null ungroups. Locked once the
    # experiment leaves DRAFT (group is spend-attribution, not just a label).
    group_id: Optional[int] = None


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


# ---------------------------------------------------------------------------
# /api/v1 programmatic read API
# ---------------------------------------------------------------------------
class V1RatingRow(BaseModel):
    """One human rating with its full question context.

    JSON equivalent of a CSV export row: full (untruncated) question text and
    ground truth, plus the rater and their answer/timing.
    """

    rating_id: int
    # The dataset-provided question identifier (string) plus our internal PK,
    # so a client can join back to whichever it holds.
    question_id: str
    question_db_id: int
    question_text: str
    gt_answer: Optional[str] = None
    options: Optional[str] = None
    question_type: str
    rater_prolific_id: str
    rater_study_id: Optional[str] = None
    rater_session_id: Optional[str] = None
    is_preview: bool
    answer: str
    confidence: int
    time_started: datetime
    time_submitted: datetime
    response_time_seconds: float
    # False for overshoot ratings beyond num_ratings_per_question (matches the
    # export's ranking) so analysis can truncate to the intended target.
    counts_toward_target: bool


class V1RatingsPage(BaseModel):
    experiment_id: int
    # Total matching ratings, not just this page: page to `offset >= total`.
    total: int
    limit: int
    offset: int
    ratings: list[V1RatingRow]


class V1ExperimentResponse(BaseModel):
    """Public projection of an experiment for the /api/v1 API.

    Deliberately narrower than ExperimentResponse: internal-only fields
    (internal_name, spend, attention flags, dataset filenames, Prolific/prompt
    config) are omitted so a bearer-key holder — and the public OpenAPI doc —
    never sees them.
    """

    id: int
    name: str
    created_at: datetime
    status: ExperimentStatus
    num_ratings_per_question: int
    question_count: int
    rating_count: int
    archived_at: Optional[datetime] = None
    assistance_method: str = "none"
    description: Optional[str] = None


# --- Datasets (identity anchor for experiment grouping) --------------------
# Wave tokens are short enum-like identifiers ("fall25", "sp26"). They are
# normalized to lowercase in the service layer so a group's attribution wave
# (validated against this set) can never miss on casing.
WaveToken = Annotated[str, Field(min_length=1, max_length=64)]
MAX_WAVES_PER_DATASET = 20


class DatasetCreate(BaseModel):
    # Strip before length validation so a whitespace-only name is rejected as
    # empty. Internal casing/punctuation is preserved — for pipeline datasets
    # the name must match the card name verbatim (cross-repo join key).
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=255)
    waves: list[WaveToken] = Field(default_factory=list, max_length=MAX_WAVES_PER_DATASET)


class DatasetUpdate(BaseModel):
    # None = leave unchanged; both fields optional so PATCH is partial.
    model_config = ConfigDict(str_strip_whitespace=True)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    waves: Optional[list[WaveToken]] = Field(default=None, max_length=MAX_WAVES_PER_DATASET)


class DatasetResponse(BaseModel):
    id: int
    name: str
    waves: list[str] = Field(default_factory=list)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExperimentGroupCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=255)
    dataset_id: int
    # Omit to auto-fill when the dataset's wave set is a singleton.
    wave: Optional[WaveToken] = None


class ExperimentGroupUpdate(BaseModel):
    # None = leave unchanged. `dataset_id` / `wave` are rejected once any
    # experiment in the group has left DRAFT.
    model_config = ConfigDict(str_strip_whitespace=True)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    dataset_id: Optional[int] = None
    wave: Optional[WaveToken] = None


class ExperimentGroupResponse(BaseModel):
    id: int
    name: str
    dataset_id: int
    dataset_name: str
    wave: str
    experiment_count: int = 0
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- API key management (dashboard, cookie-authed) -------------------------
class ApiKeyCreate(BaseModel):
    # Strip before length validation so a whitespace-only name (" ") is rejected
    # as empty rather than stored as a blank, unlabeled key.
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=255)


class ApiKeyResponse(BaseModel):
    id: int
    name: str
    # Non-secret display form: prefix + a masked tail. The full key is never
    # returned after creation.
    masked_key: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_by: Optional[str] = None
    is_active: bool


class ApiKeyCreated(ApiKeyResponse):
    # The full secret, returned exactly once (create or regenerate).
    plaintext_key: str
