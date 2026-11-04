"""What a rater is shown for one question, built from the rater endpoints'
own helpers so the check sees what they serve.

`build_question_response` is the mapper `/raters/next-question` uses; the
prefix and suffix come from the experiment row as `/raters/start` sends them.
A change to either changes what the check reads.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Experiment, Question
from schemas import QuestionResponse
from services.rater.mappers import build_question_response


@dataclass
class RaterQuestionView:
    response: QuestionResponse
    human_prompt_prefix: str | None
    human_prompt_suffix: str | None
    # The row itself, for the method call; never shown to the judge beyond
    # what `response` carries (ground truth stays out).
    question: Question

    @property
    def question_id(self) -> int:
        return self.response.id

    def as_text(self) -> str:
        """The rater's card as labelled plain text, in display order."""
        mode = "Markdown" if self.response.is_markdown else "plain text"
        parts = [f"[Rendering mode: {mode}]"]
        if self.human_prompt_prefix:
            parts.append(f"[Shown above the question]\n{self.human_prompt_prefix}")
        if self.response.parent_question_text:
            parts.append(f"[Context]\n{self.response.parent_question_text}")
        parts.append(
            f"[Question] (type: {self.response.question_type})\n{self.response.question_text}"
        )
        if self.response.options:
            parts.append(f"[Options, as stored]\n{self.response.options}")
        if self.human_prompt_suffix:
            parts.append(f"[Shown below the question]\n{self.human_prompt_suffix}")
        return "\n\n".join(parts)


async def build_views(
    experiment: Experiment, questions: list[Question], db: AsyncSession
) -> list[RaterQuestionView]:
    parent_ids = {q.parent_question_id for q in questions if q.parent_question_id is not None}
    parent_text: dict[int, str] = {}
    if parent_ids:
        rows = await db.execute(
            select(Question.id, Question.question_text).where(Question.id.in_(parent_ids))
        )
        parent_text = {row.id: row.question_text for row in rows}
    return [
        RaterQuestionView(
            response=build_question_response(
                question,
                experiment.is_markdown,
                parent_text.get(question.parent_question_id)
                if question.parent_question_id is not None
                else None,
            ),
            human_prompt_prefix=experiment.human_prompt_prefix,
            human_prompt_suffix=experiment.human_prompt_suffix,
            question=question,
        )
        for question in questions
    ]
