"""Top-N answer assistance method.

The method asks an LLM to rank likely answers for the current question and
returns the top N candidates as static guidance. It is intentionally one-shot:
raters can review the suggestions, then make their own final rating.

assistance_params:
    model: LLM to use for ranking (default: settings.llm.default_model)
    n:     Number of candidates to show (default: 3, range 1-10)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import openai

from config import get_settings
from models import Question
from question_options import parse_options

from ..base import AssistanceMethod, InteractionStep, StepType
from ..llm import complete

logger = logging.getLogger(__name__)

_DEFAULT_TOP_N = 3
_MAX_TOP_N = 10

_SYSTEM_PROMPT = """\
You help human raters answer evaluation questions. Rank the most likely answers
without hiding uncertainty. Use only the question and options provided by the
user; do not invent options for multiple-choice questions.

Return JSON only, matching one of these shapes.

Multiple-choice (options were provided in the user prompt):
{"candidates":[{"option_index":<int>,"confidence":0-100,"rationale":"short reason"}]}
option_index is 1-based and must match the numbering shown in the user prompt.

Free-response (no options):
{"candidates":[{"answer":"...","confidence":0-100,"rationale":"short reason"}]}

confidence is your own calibrated probability (0-100) that the candidate is the
correct answer. List candidates from highest to lowest confidence.
"""


def _clamp_top_n(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = _DEFAULT_TOP_N
    return max(1, min(_MAX_TOP_N, n))


def _strip_markdown_json(raw: str) -> str:
    return re.sub(r"```json?\n?|```\n?", "", raw).strip()


def _parse_top_n_response(raw: str) -> dict:
    content = _strip_markdown_json(raw)
    decoder = json.JSONDecoder()
    for start_index, char in enumerate(content):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(content[start_index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("candidates"), list):
            return parsed
    raise json.JSONDecodeError("No top-N candidates JSON object found", content, 0)


def _normalize_candidates(raw_candidates: Any, options: list[str], n: int) -> list[dict[str, Any]]:
    """Validate LLM candidates, order them by descending confidence, keep top n.

    Confidence decides everything: the list is sorted before it is deduplicated
    and truncated, so a repeated answer keeps its highest-confidence entry and a
    strong candidate the LLM listed late is not dropped. The sort is stable, so
    equal-confidence candidates keep the LLM's own ordering. Ranks are assigned
    last, so `rank` always agrees with the displayed order.
    """
    if not isinstance(raw_candidates, list):
        return []

    parsed: list[dict[str, Any]] = []

    for item in raw_candidates:
        if not isinstance(item, dict):
            continue

        if options:
            try:
                option_index = int(item["option_index"])
            except (KeyError, TypeError, ValueError):
                logger.info("Dropping top-N candidate with missing/invalid option_index: %r", item)
                continue
            if not 1 <= option_index <= len(options):
                logger.info(
                    "Dropping top-N candidate with out-of-range option_index: %r", option_index
                )
                continue
            answer = options[option_index - 1]
        else:
            answer = str(item.get("answer", "")).strip()
            if not answer:
                continue

        try:
            confidence = int(item.get("confidence", 50))
        except (TypeError, ValueError):
            confidence = 50

        parsed.append(
            {
                "answer": answer,
                "confidence": max(0, min(100, confidence)),
                "rationale": str(item.get("rationale", "")).strip(),
            }
        )

    parsed.sort(key=lambda candidate: candidate["confidence"], reverse=True)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in parsed:
        dedupe_key = candidate["answer"].casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        candidate["rank"] = len(candidates) + 1
        candidates.append(candidate)
        if len(candidates) >= n:
            break

    return candidates


def _compose_system_prompt(extra: str | None) -> str:
    """Append a researcher-supplied system prompt to the method's own.

    Kept after the method prompt so the JSON-output contract stays last and
    is least likely to be overridden by free-form study framing.
    """
    if not extra or not extra.strip():
        return _SYSTEM_PROMPT
    return f"{_SYSTEM_PROMPT}\n\nStudy-specific context:\n{extra.strip()}"


class TopNAssistance(AssistanceMethod):
    async def start(
        self,
        question: Question,
        params: dict,
        *,
        parent_question_text: str | None = None,
        experiment_system_prompt: str | None = None,
    ) -> InteractionStep:
        settings = get_settings()
        model = params.get("model") or settings.llm.default_model
        requested_n = _clamp_top_n(params.get("n", _DEFAULT_TOP_N))
        options = parse_options(question.options)
        n = min(requested_n, len(options)) if options else requested_n

        option_block = (
            "\n".join(f"{idx + 1}. {option}" for idx, option in enumerate(options))
            if options
            else "(free-response question; propose concise candidate answers)"
        )
        context_block = (
            f"Parent question/context:\n{parent_question_text}\n\n" if parent_question_text else ""
        )
        return_instruction = (
            f"Return exactly the top {n} candidate(s) as option_index values "
            f"(1-based, matching the numbering above), ordered from highest to "
            f"lowest confidence."
            if options
            else f"Return exactly the top {n} candidate answer(s) as free text, "
            f"ordered from highest to lowest confidence."
        )
        user_prompt = (
            f"{context_block}"
            f"Question:\n{question.question_text}\n\n"
            f"Question type: {question.question_type}\n"
            f"Options:\n{option_block}\n\n"
            f"{return_instruction}"
        )

        try:
            raw = await complete(
                [
                    {"role": "system", "content": _compose_system_prompt(experiment_system_prompt)},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                settings=settings.llm,
                response_format={"type": "json_object"},
                temperature=0,
            )
        except (RuntimeError, ValueError, openai.OpenAIError):
            logger.exception("Top-N LLM call failed; returning no-assistance step")
            return InteractionStep(type=StepType.NONE, is_terminal=True)

        try:
            parsed = _parse_top_n_response(raw)
        except json.JSONDecodeError:
            logger.warning("Failed to parse top-N assistance response: %r", raw)
            parsed = {}

        candidates = _normalize_candidates(parsed.get("candidates"), options, n)
        if not candidates:
            return InteractionStep(type=StepType.NONE, is_terminal=True)

        return InteractionStep(
            type=StepType.DISPLAY,
            payload={
                "kind": "top_n",
                "top_n": n,
                "candidates": candidates,
                "has_options": bool(options),
            },
            is_terminal=True,
        )
