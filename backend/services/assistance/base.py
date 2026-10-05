"""Base class and data types for assistance methods."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from models import Question, StepType

from .model_resolution import role_default

if TYPE_CHECKING:
    from .preparation import PreparationContext, PreparationSpec, QuestionSnapshot

FailureReason = Literal["provider_error", "invalid_response", "execution_error"]

__all__ = ["AssistanceMethod", "InteractionStep", "StepType"]


@dataclass
class InteractionStep:
    """Represents one step in an assistance interaction.

    payload:
        What the frontend renders. Sent to the client in AssistanceStepResponse.
    state:
        Backend-only memory between turns. Persisted and passed back to
        advance() on the next call. Never sent to the frontend.
        For one-shot (terminal) methods this can be left empty.
    is_terminal:
        True when no further advance() call is expected.
    """

    type: StepType
    payload: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    is_terminal: bool = False
    # Research attribution only; never sent in the participant payload.
    failure_reason: FailureReason | None = None

    @property
    def outcome(self) -> str:
        return self.failure_reason or (
            "no_assistance" if self.type == StepType.NONE else "provided"
        )


class AssistanceMethod(ABC):
    """Interface every assistance method must implement.

    One-shot methods only need to override start(); multi-turn methods
    override both start() and advance().

    Subclasses should set ``rater_instructions`` to a short plain-text
    description of how the method works from the rater's perspective.
    It is shown to raters on the intro screen before they begin rating.
    Leave as the empty string to suppress the box.
    """

    rater_instructions: str = ""

    # Which `model_resolution` role this method's rater-facing call runs on;
    # None for a method that calls no model. Read without instantiation, like
    # `rater_instructions`, so a session can record the model that actually ran.
    primary_model_role: str | None = None

    @classmethod
    def default_model(cls) -> str | None:
        """The model this method runs on without an `assistance_models` entry.

        None for a method that calls no model.
        """
        return role_default(cls.primary_model_role) if cls.primary_model_role else None

    def plan_preparation(self, context: PreparationContext) -> PreparationSpec | None:
        """Declare optional work safe to compute before the question is displayed.

        Planning must be deterministic and have no side effects. Methods that
        opt in also implement prepare() and consume_preparation(). The service
        owns participant isolation, persistence, and execution limits.
        """
        return None

    async def prepare(self, spec: PreparationSpec) -> dict:
        """Produce a private JSON artifact without anticipating human input."""
        raise NotImplementedError

    async def consume_preparation(self, spec: PreparationSpec, artifact: dict) -> InteractionStep:
        """Validate and use prepared work; never silently regenerate it."""
        raise NotImplementedError

    @abstractmethod
    async def start(
        self,
        question: Question | QuestionSnapshot,
        params: dict,
        *,
        parent_question_text: str | None = None,
        experiment_system_prompt: str | None = None,
    ) -> InteractionStep:
        """Begin an assistance interaction for the given question.

        parent_question_text:
            If the question is a sub-question (the upload row's
            parent_question_id is populated), this is the parent row's
            question_text — the same
            context shown to the rater above the question. Methods that pass
            the question to an LLM should incorporate this; otherwise the
            model loses the context the rater can see.
        experiment_system_prompt:
            Dataset-level system prompt declared by the researcher (via the
            upload's dataset metadata or the admin UI). Methods that drive an LLM
            should append it to their own system prompt so the model gets
            study-specific framing on top of the method's task structure.
        """
        ...

    async def advance(
        self,
        state: dict,
        human_input: str,
        params: dict,
        *,
        experiment_system_prompt: str | None = None,
    ) -> InteractionStep:
        """Advance a multi-turn interaction with the rater's latest input.

        The default implementation raises, signalling that this method is
        terminal after start(). Stateful methods should override this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support multi-turn interactions."
        )
