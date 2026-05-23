"""Base class and data types for assistance methods."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from models import Question, StepType

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


class AssistanceMethod(ABC):
    """Interface every assistance method must implement.

    One-shot methods only need to override start(); multi-turn methods
    override both start() and advance().
    """

    @abstractmethod
    async def start(
        self,
        question: Question,
        params: dict,
        *,
        parent_question_text: str | None = None,
    ) -> InteractionStep:
        """Begin an assistance interaction for the given question.

        parent_question_text:
            If the question is a sub-question (CSV column parent_question_id
            populated), this is the parent row's question_text — the same
            context shown to the rater above the question. Methods that pass
            the question to an LLM should incorporate this; otherwise the
            model loses the context the rater can see.
        """
        ...

    async def advance(self, state: dict, human_input: str, params: dict) -> InteractionStep:
        """Advance a multi-turn interaction with the rater's latest input.

        The default implementation raises, signalling that this method is
        terminal after start(). Stateful methods should override this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support multi-turn interactions."
        )
