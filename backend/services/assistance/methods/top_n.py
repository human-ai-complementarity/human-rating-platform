"""Top-N answer assistance method.

The method asks an LLM to rank likely answers for the current question and
returns the top N candidates as static guidance. It is intentionally one-shot:
raters can review the suggestions, then make their own final rating.

Candidates are returned in a random order rather than best-first, and the UI
shows neither the rank nor the model's confidence, so the rater sees the
shortlist without the model's ordering anchoring their choice. Each candidate
still carries its `rank`, persisted with the session payload for analysis.

assistance_params:
    model: LLM to use for ranking (default: settings.llm.default_model)
    n:     Number of candidates to show (default: 3, range 1-10)
"""

from __future__ import annotations

import json
import logging
import random
import re
from typing import Any

import openai

from config import get_settings
from models import Question

from ..base import AssistanceMethod, InteractionStep, StepType
from ..llm import complete

logger = logging.getLogger(__name__)

_DEFAULT_TOP_N = 3
_MAX_TOP_N = 10
_OPTION_LABEL_PATTERN = re.compile(r"(?:^|[,\r\n])\s*(?:\(?[A-Z]\)?[.)]|[A-Z]:)\s+")

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
"""


def _parse_options(raw_options: str | None) -> list[str]:
    if not raw_options:
        return []

    if "|" in raw_options:
        return [option.strip() for option in raw_options.split("|") if option.strip()]

    labeled_option_starts = [match.start() for match in _OPTION_LABEL_PATTERN.finditer(raw_options)]
    if len(labeled_option_starts) > 1:
        options = []
        for index, start in enumerate(labeled_option_starts):
            end = (
                labeled_option_starts[index + 1] if index + 1 < len(labeled_option_starts) else None
            )
            option = raw_options[start:end].strip(" ,\r\n")
            if option:
                options.append(option)
        return options

    line_options = [option.strip() for option in re.split(r"\r?\n+", raw_options) if option.strip()]
    if len(line_options) > 1:
        return line_options

    return [option.strip() for option in raw_options.split(",") if option.strip()]


def _clamp_top_n(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = _DEFAULT_TOP_N
    return max(1, min(_MAX_TOP_N, n))


def _strip_markdown_json(raw: str) -> str:
    return re.sub(r"```json?\n?|```\n?", "", raw).strip()


def _salvage_candidates(content: str) -> list[dict]:
    """Collect standalone candidate objects when the wrapper object won't decode.

    A single malformed token anywhere in the response makes the enclosing
    ``{"candidates": [...]}`` undecodable, even though the individual candidate
    objects on either side of it are intact. Observed in production against
    claude-sonnet-4-6, which emits ``"confidence">80`` (a comparison rather than
    a value) on the middle, hedged candidate; the first and last candidates
    parse cleanly but were being discarded with it.

    Only ever called once the wrapper scan has come up empty, so a response that
    parses today never reaches this and its result is unchanged.

    Note the recovered set is biased: the dropped candidate is the one the model
    hedged on, which is disproportionately the one proposing a *different*
    answer. Callers get fewer, less diverse suggestions rather than none.
    """
    decoder = json.JSONDecoder()
    salvaged: list[dict] = []
    index = 0
    while index < len(content):
        if content[index] != "{":
            index += 1
            continue
        try:
            parsed, end = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        # Candidate shape per the method's own contract: free-response entries
        # carry "answer", multiple-choice entries carry "option_index".
        if isinstance(parsed, dict) and ("answer" in parsed or "option_index" in parsed):
            salvaged.append(parsed)
            index += end
            continue
        index += 1
    return salvaged


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

    salvaged = _salvage_candidates(content)
    if salvaged:
        logger.warning(
            "Top-N wrapper JSON was malformed; salvaged %d candidate(s) from %r",
            len(salvaged),
            content,
        )
        return {"candidates": salvaged}
    raise json.JSONDecodeError("No top-N candidates JSON object found", content, 0)


def _normalize_candidates(raw_candidates: Any, options: list[str], n: int) -> list[dict[str, Any]]:
    if not isinstance(raw_candidates, list):
        return []

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

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

        dedupe_key = answer.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        try:
            confidence = int(item.get("confidence", 50))
        except (TypeError, ValueError):
            confidence = 50

        candidates.append(
            {
                "rank": len(candidates) + 1,
                "answer": answer,
                "confidence": max(0, min(100, confidence)),
                "rationale": str(item.get("rationale", "")).strip(),
            }
        )
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
        options = _parse_options(question.options)
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
            f"(1-based, matching the numbering above), ordered best first."
            if options
            else f"Return exactly the top {n} candidate answer(s) as free text, ordered best first."
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

        # Payload order is display order, so shuffle here rather than in the UI:
        # the shuffled list is what gets persisted with the session, which keeps
        # the record of what the rater actually saw.
        shuffled = random.sample(candidates, len(candidates))

        return InteractionStep(
            type=StepType.DISPLAY,
            payload={
                "kind": "top_n",
                "top_n": n,
                "candidates": shuffled,
                "has_options": bool(options),
            },
            is_terminal=True,
        )
