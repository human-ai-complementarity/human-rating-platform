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
Your goal is to decompose a question into atomic sub-questions whose answers, taken together, are sufficient to answer the original question — then provide your best current answer to each one, grounded in specific evidence from the source if applicable.

Important: every subtask you produce — its question, my_answer, and evidence quote — will be reviewed by a human grader and your evidence quotes will be checked verbatim against the source. Fabricated or inaccurate quotes will be flagged. Honest uncertainty (empty evidence with a hedged my_answer) is strictly preferable to a confident answer you cannot substantiate.

Step 1 — In ONE sentence, state the EVALUATION OBJECTIVE: what specific judgement, determination, or fact is the question asking for? Examples:
  - For a factual question: the objective is to retrieve the requested fact.
  - For a yes/no judgement (e.g. "did the assistant deviate?"): the objective is to determine whether the condition holds.
  - For a multiple-choice question: the objective is to select the correct option from those given.

Step 2 — Identify atomic sub-questions whose answers, taken together, are sufficient to make that determination. Each sub-question must directly contribute to the objective from Step 1. Do NOT produce sub-questions about surface content unrelated to the evaluation objective.

Avoid redundant decomposition: do NOT produce a sub-question that merely restates the original question, paraphrases the meta-judgement, or asks again whether a central claim explicit in the source is true. If multiple overlapping checks would all answer the meta-question directly (e.g. "did the assistant deviate?", "did the assistant's claims hold up?", "is the final response wrong?"), aggregate them into a SINGLE sub-question that captures the meta-check, and use the remaining sub-questions for the specific factual claims that have to be verified to support that meta-check.

Consider alternatives WHEN APPLICABLE: before finalising your subtasks, briefly check whether this question has a meaningful alternative interpretation — for example the opposite verdict in a yes/no judgements, a less charitable reading (in deception or behaviour-evaluation tasks), an alternative option in multiple-choice options, or a plausible reading you might have missed. If a substantive alternative exists, include at least ONE sub-question whose answer would distinguish your initial leaning from that alternative. Skip this step entirely for questions with a single canonical answer where no meaningful alternative applies. The point is to guard against confidently accepting your initial framing on questions that genuinely have a less-obvious alternative reading.

Step 3 — For each sub-question, fill in "my_answer" following the schema rules below AND fill in "evidence" with EXACTLY ONE of the following:
  (i) A SHORT verbatim quote (<= 200 characters) from the source material that directly supports your my_answer. Use this when the source contains text that decides the answer.
  (ii) A SHORT justification (<= 200 characters) prefixed with the literal marker "[knowledge]" — but ONLY for trivial, universally-verifiable world knowledge (well-known dates, geography, definitions, simple arithmetic). Do NOT use "[knowledge]" for judgements, inferences, multi-step reasoning, or specialized/technical claims — for those, quote the source (i) or leave evidence empty (iii). 
  (iii) The empty string "" — and ONLY this — when you cannot substantiate the my_answer from either a source quote OR trivial, universally-verifiable world knowledge. In this case, prefer a more hedged or uncertain my_answer rather than a confident one.

Do NOT fabricate quotes. Do NOT use the "[knowledge]" marker as a fallback to avoid quoting when a real quote is available — quotes are preferred when they apply. An empty evidence field is a strong signal that the human should review the subtask.

You must always return subtasks — never synthesise on the first pass. The human must always have the opportunity to review and correct your answers.

{subtask_schema}

In addition to the schema fields above, every subtask must also include:
  - "evidence": one of (a) a verbatim source quote, (b) a "[knowledge]"-prefixed brief ONLY for trivial, universally-verifiable world knowledge, or (c) "" when neither applies (see Step 3).

Respond with JSON only — no explanation, no markdown fences.

Always respond with:
{{"done": false, "evaluation_objective": "<one-sentence statement from Step 1>", "subtasks": [/* up to {max_subtasks} subtask objects, each with the schema fields PLUS an "evidence" field */]}}\
"""

_ADVANCE_SYSTEM = """\
You are working toward answering a question across multiple rounds. Each round you either synthesise a final answer or decompose remaining uncertainty into new sub-questions.

This is round {iteration} of {max_rounds} maximum.{forced_note}

The human has reviewed and corrected your previous sub-question answers. Incorporate their input and decide:

1. If you now have enough information to answer the original question:
   {{"done": true, "synthesis": {{"reasoning": "<step-by-step explanation>", "answer": "<final answer>"}}}}

2. If there is still remaining uncertainty that the human can help resolve — decompose it into new atomic sub-questions, exactly as you did in the first round. For each new sub-question, provide your best current answer regardless of confidence. Do not repeat sub-questions that have already been addressed.
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
