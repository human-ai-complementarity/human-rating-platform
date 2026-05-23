"""No-op assistance method (default when no assistance is configured)."""

from __future__ import annotations

from models import Question
from ..base import AssistanceMethod, InteractionStep, StepType


class NoAssistance(AssistanceMethod):
    async def start(
        self,
        question: Question,
        params: dict,
        *,
        parent_question_text: str | None = None,
    ) -> InteractionStep:
        return InteractionStep(type=StepType.NONE, is_terminal=True)
