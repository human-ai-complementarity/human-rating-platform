"""Method-owned preparation, independent of scheduling and database sessions."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from pydantic import BaseModel, ConfigDict, Field

from models import Question, StepType

from .base import AssistanceMethod, InteractionStep, FailureReason


@dataclass(frozen=True)
class QuestionSnapshot:
    """Only inputs a method may see. Deliberately excludes ground truth/metadata."""

    id: int
    experiment_id: int
    question_text: str
    question_type: str
    options: str | None

    @classmethod
    def capture(cls, question: Question) -> QuestionSnapshot:
        return cls(
            id=question.id,
            experiment_id=question.experiment_id,
            question_text=question.question_text,
            question_type=question.question_type,
            options=question.options,
        )


@dataclass(frozen=True)
class PreparationContext:
    question: QuestionSnapshot
    # Deterministic JSON, rather than a frozen dataclass containing mutable dicts.
    params_json: str
    parent_question_text: str | None = None
    experiment_system_prompt: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "params_json", json.dumps(json.loads(self.params_json), sort_keys=True)
        )

    @property
    def params(self) -> dict:
        return json.loads(self.params_json)


@dataclass(frozen=True)
class PreparationSpec:
    """One versioned unit of method-owned work, with self-contained inputs."""

    name: str
    version: int
    inputs_json: str


class InitialStepArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: StepType
    payload: dict = Field(default_factory=dict)
    state: dict = Field(default_factory=dict)
    is_terminal: bool
    failure_reason: FailureReason | None = None
    failure_detail: str | None = None


class InitialStepPreparation(AssistanceMethod):
    """Adapter for methods whose entire first step can be computed early.

    Foreground start() remains the single implementation of method behavior.
    Bump preparation_version when changing inputs or artifact interpretation.
    Multi-turn advance() is intentionally outside this contract.
    """

    preparation_version = 1

    def plan_preparation(self, context: PreparationContext) -> PreparationSpec:
        return PreparationSpec(
            name="initial_step",
            version=self.preparation_version,
            inputs_json=json.dumps(asdict(context), sort_keys=True),
        )

    def _validate_spec(self, spec: PreparationSpec) -> None:
        if spec.name != "initial_step" or spec.version != self.preparation_version:
            raise ValueError("Incompatible initial-step preparation")

    async def prepare(self, spec: PreparationSpec) -> dict:
        self._validate_spec(spec)
        values = json.loads(spec.inputs_json)
        context = PreparationContext(
            **{**values, "question": QuestionSnapshot(**values["question"])}
        )
        step = await self.start(
            context.question,
            context.params,
            parent_question_text=context.parent_question_text,
            experiment_system_prompt=context.experiment_system_prompt,
        )
        return InitialStepArtifact(**asdict(step)).model_dump(mode="json")

    async def consume_preparation(self, spec: PreparationSpec, artifact: dict) -> InteractionStep:
        self._validate_spec(spec)
        # JSON validation checks enum strings while retaining strict booleans.
        parsed = InitialStepArtifact.model_validate_json(json.dumps(artifact))
        return InteractionStep(**parsed.model_dump())
