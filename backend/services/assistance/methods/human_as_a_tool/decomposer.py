"""Subtask decomposer for the human-as-a-tool method.

Handles all LLM calls for decomposing a question into subtasks and
synthesising a final answer. Confidence scoring is intentionally absent
here — scores are assigned by a separate ConfidenceEstimator after decomposition.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from config import LLMSettings, get_settings

from ...llm import complete

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SUBTASK_SCHEMA = """\
Subtask schema:
{{
  "index": <integer starting at 0>,
  "question": "<atomic sub-question>",
  "type": "binary" | "multiple_choice" | "free_text",
  "options": ["opt1", "opt2", ...] | null,
  "my_answer": <see rules below>,
  "my_answer_index": <see rules below>
}}

my_answer / my_answer_index rules — follow exactly, no exceptions:
- binary:          my_answer = exactly "yes" or "no" (lowercase). my_answer_index = null.
- multiple_choice: my_answer_index = 0-based index of your chosen option in "options". my_answer = "".
- free_text:       my_answer = a concise answer, no explanation appended. my_answer_index = null.

The human sees my_answer as a pre-filled response. It must be usable by the UI directly.\
"""

_START_SYSTEM = """\
Your goal is to decompose a question into all of the atomic sub-questions that \
together are sufficient to answer it — then provide your best current answer to \
each one, regardless of how confident you are.

Step 1 — Identify every specific fact, judgement, or clarification that must be \
established to answer the question. Include sub-questions you already know the \
answer to. Do not pre-filter by confidence.

Step 2 — For each sub-question, fill in "my_answer" following the schema rules \
below exactly.

You must always return subtasks — never synthesise on the first pass. \
The human must always have the opportunity to review and correct your answers.

{subtask_schema}

Respond with JSON only — no explanation, no markdown fences.

Always respond with:
{{"done": false, "subtasks": [/* up to {max_subtasks} subtask objects */]}}\
"""

_ADVANCE_SYSTEM = """\
You are working toward answering a question across multiple rounds. Each round \
you either synthesise a final answer or decompose remaining uncertainty into new sub-questions.

This is round {iteration} of {max_rounds} maximum.{forced_note}

The human has reviewed and corrected your previous sub-question answers. \
Incorporate their input and decide:

1. If you now have enough information to answer the original question:
   {{"done": true, "synthesis": {{"answer": "<answer>", "reasoning": "<step-by-step explanation>"}}}}

2. If there is still remaining uncertainty that the human can help resolve — \
decompose it into new atomic sub-questions, exactly as you did in the first round. \
For each new sub-question, provide your best current answer regardless of confidence. \
Do not repeat sub-questions that have already been addressed.
   {{"done": false, "subtasks": [/* new subtask objects only */]}}

{subtask_schema}

Respond with JSON only — no explanation, no markdown fences.\
"""

_FORCED_NOTE = (
    " This is the final round — you MUST synthesise now regardless of remaining uncertainty."
)

_FALLBACK_SYNTHESIS_SYSTEM = """\
Based on the information gathered so far, provide your best answer to the question.

Respond with JSON only — no explanation, no markdown fences:
{{"answer": "<your best answer>", "reasoning": "<step-by-step explanation>"}}\
"""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class DecompositionResult:
    done: bool
    subtasks: list[dict] = field(default_factory=list)
    synthesis: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def format_history(history: list[dict]) -> str:
    lines = []
    for i, round_ in enumerate(history, 1):
        lines.append(f"Round {i}:")
        for st in round_["subtasks"]:
            raw = round_["answers"].get(str(st["index"]), "(no answer)")
            if isinstance(raw, dict):
                ans_str = raw.get("answer") or "(no answer)"
                conf = raw.get("confidence")
                human_answer = f"{ans_str} (confidence: {conf}/5)" if conf is not None else ans_str
            else:
                human_answer = raw
            lines.append(f"  Uncertainty: {st['question']}")
            lines.append(f"  My answer:   {st.get('my_answer', '(none)')}")
            lines.append(f"  Human input: {human_answer}")
    return "\n".join(lines)


def _build_user_msg(question_text: str, options: str, history: list[dict] | None = None) -> str:
    msg = f"Question: {question_text}"
    if options:
        msg += f"\nAnswer options: {options}"
    if history:
        msg += f"\n\nInformation gathered so far:\n{format_history(history)}"
    return msg


def _parse_response(raw: str, context: str) -> dict:
    content = re.sub(r"```json?\n?|```\n?", "", raw).strip()
    try:
        return json.loads(content)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Failed to parse %s response: %r", context, raw)
        return {}


def _normalize_subtasks(subtasks: list[dict]) -> list[dict]:
    """Enforce my_answer format per subtask type.

    The LLM sometimes appends reasoning to my_answer despite prompt instructions.
    This is the authoritative normalization — the frontend must not need to do this.

    - binary:          extract leading 'yes'/'no' word, capitalise
    - multiple_choice: find the option that my_answer starts with (case-insensitive)
    - free_text:       leave as-is
    """
    normalized = []
    for st in subtasks:
        answer = (st.get("my_answer") or "").strip()
        stype = st.get("type")

        if stype == "binary":
            lower = answer.lower()
            if lower.startswith("yes"):
                answer = "yes"
            elif lower.startswith("no"):
                answer = "no"
            else:
                logger.warning("binary my_answer %r does not start with yes/no", answer)

        elif stype == "multiple_choice":
            options: list[str] = st.get("options") or []
            idx = st.get("my_answer_index")
            if isinstance(idx, int) and 0 <= idx < len(options):
                answer = options[idx]
            else:
                logger.warning(
                    "multiple_choice my_answer_index %r is invalid for options %r", idx, options
                )

        normalized.append({**st, "my_answer": answer})
    return normalized


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


def _append_experiment_system_prompt(base: str, extra: str | None) -> str:
    """Append the researcher's dataset-level system prompt to a method prompt.

    Kept after the method prompt so the JSON-output contract and decomposition
    rules stay last and are least likely to be overridden by study framing.
    """
    if not extra or not extra.strip():
        return base
    return f"{base}\n\nStudy-specific context:\n{extra.strip()}"


class SubtaskDecomposer:
    async def start(
        self,
        question_text: str,
        options: str,
        max_subtasks: int,
        model: str | None = None,
        experiment_system_prompt: str | None = None,
    ) -> DecompositionResult:
        settings = get_settings()
        system = _append_experiment_system_prompt(
            _START_SYSTEM.format(subtask_schema=_SUBTASK_SCHEMA, max_subtasks=max_subtasks),
            experiment_system_prompt,
        )
        user_msg = _build_user_msg(question_text, options)

        raw = await complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            model=model,
            settings=settings.llm,
        )

        parsed = _parse_response(raw, "start")
        if not parsed:
            return DecompositionResult(done=True)

        if parsed.get("done"):
            return DecompositionResult(done=True, synthesis=parsed.get("synthesis", {}))

        subtasks = _normalize_subtasks(parsed.get("subtasks", []))
        if not subtasks:
            logger.warning("start() returned done=false with no subtasks")
            return DecompositionResult(done=True)

        return DecompositionResult(done=False, subtasks=subtasks)

    async def advance(
        self,
        question_text: str,
        options: str,
        history: list[dict],
        iteration: int,
        max_rounds: int,
        model: str | None = None,
        experiment_system_prompt: str | None = None,
    ) -> DecompositionResult:
        settings = get_settings()
        is_final = iteration >= max_rounds
        forced_note = _FORCED_NOTE if is_final else ""

        system = _append_experiment_system_prompt(
            _ADVANCE_SYSTEM.format(
                iteration=iteration,
                max_rounds=max_rounds,
                forced_note=forced_note,
                subtask_schema=_SUBTASK_SCHEMA,
            ),
            experiment_system_prompt,
        )
        user_msg = _build_user_msg(question_text, options, history)

        raw = await complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            model=model,
            settings=settings.llm,
        )

        parsed = _parse_response(raw, "advance")
        if not parsed:
            logger.error(
                "advance() received unparseable LLM response: iteration=%s, history_rounds=%s, raw=%r",
                iteration,
                len(history),
                raw[:200],
                exc_info=True,
            )
            raise RuntimeError("LLM returned an unparseable response")

        if parsed.get("done") or is_final:
            synthesis = parsed.get("synthesis") or {}
            if not synthesis.get("answer"):
                logger.warning(
                    "advance() forced synthesis but LLM omitted it; making fallback call"
                )
                synthesis = await self._fallback_synthesize(
                    question_text,
                    options,
                    history,
                    model,
                    settings.llm,
                    experiment_system_prompt,
                )
            return DecompositionResult(done=True, synthesis=synthesis)

        subtasks = _normalize_subtasks(parsed.get("subtasks", []))
        if not subtasks:
            logger.warning("advance() returned done=false with no subtasks; forcing synthesis")
            synthesis = await self._fallback_synthesize(
                question_text,
                options,
                history,
                model,
                settings.llm,
                experiment_system_prompt,
            )
            return DecompositionResult(done=True, synthesis=synthesis)

        return DecompositionResult(done=False, subtasks=subtasks)

    async def _fallback_synthesize(
        self,
        question_text: str,
        options: str,
        history: list[dict],
        model: str,
        llm_settings: LLMSettings,
        experiment_system_prompt: str | None = None,
    ) -> dict:
        """Make a dedicated synthesis call when the LLM failed to include one."""
        user_msg = _build_user_msg(question_text, options, history)
        raw = await complete(
            [
                {
                    "role": "system",
                    "content": _append_experiment_system_prompt(
                        _FALLBACK_SYNTHESIS_SYSTEM, experiment_system_prompt
                    ),
                },
                {"role": "user", "content": user_msg},
            ],
            model=model,
            settings=llm_settings,
        )
        result = _parse_response(raw, "fallback_synthesis")
        if not result or not result.get("answer"):
            logger.error("Fallback synthesis also failed: %r", raw[:200], exc_info=True)
            return {"answer": "", "reasoning": ""}
        return result
