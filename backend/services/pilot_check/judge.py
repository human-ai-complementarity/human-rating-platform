"""The LLM judge behind the judged checks.

One call shape for every judged check: a task (the rubric), the material
(what the rater sees), and a JSON verdict back. Rubrics live with their
checks so a prompt edit is a reviewed change with a test; this module only
knows how to ask.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from config import get_settings
from services.assistance.llm import complete, missing_key_env_var

from .config import JudgeConfig

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You review the material a paid crowd rater is about to be shown on a rating \
platform, before the study goes live. You are not answering the task; you are \
checking that a careful rater could. Be concrete and brief: name the problem \
and, where you can, quote the words that cause it. Do not flag matters of \
taste. Reply with JSON only."""

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["ok", "issues", "summary"],
    "additionalProperties": False,
}

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class JudgeError(RuntimeError):
    """The judge could not be asked or did not answer in the shape asked for."""


@dataclass
class Verdict:
    ok: bool
    issues: list[str] = field(default_factory=list)
    summary: str = ""


class Judge(Protocol):
    model: str

    async def __call__(self, *, task: str, material: str) -> Verdict: ...


def parse_verdict(raw: str) -> Verdict:
    text = _FENCE.sub("", raw.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge reply was not JSON: {raw[:200]!r}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise JudgeError(f"judge reply missing a boolean 'ok': {raw[:200]!r}")
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    return Verdict(
        ok=data["ok"],
        issues=[str(issue).strip() for issue in issues if str(issue).strip()],
        summary=str(data.get("summary") or "").strip(),
    )


class LLMJudge:
    def __init__(self, config: JudgeConfig):
        self.model = config.model
        self.temperature = config.temperature

    async def __call__(self, *, task: str, material: str) -> Verdict:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"{task}\n\n"
                    f"=== Material ===\n{material}\n=== End of material ===\n\n"
                    'Answer with JSON: {"ok": true|false, "issues": ["..."], "summary": "..."}. '
                    "ok is false only when a rater would be confused, blocked or misled."
                ),
            },
        ]
        # Not every model behind a provider accepts a JSON schema (top_n probes
        # for the same reason), so step down to JSON mode, then to plain text,
        # before giving up. The reply is parsed the same way whichever worked.
        formats: list[dict | None] = [
            {
                "type": "json_schema",
                "json_schema": {"name": "verdict", "schema": _VERDICT_SCHEMA, "strict": True},
            },
            {"type": "json_object"},
            None,
        ]
        last: Exception | None = None
        for response_format in formats:
            try:
                raw = await complete(
                    messages,
                    settings=get_settings().llm,
                    model=self.model,
                    response_format=response_format,
                    temperature=self.temperature,
                )
            except Exception as exc:
                last = exc
                logger.info(
                    "judge call failed; trying a simpler response format",
                    extra={"attributes": {"format": response_format and response_format["type"]}},
                )
                continue
            return parse_verdict(raw)
        raise JudgeError(f"{type(last).__name__}: {last}")


def judge_unavailable_reason(config: JudgeConfig) -> str | None:
    """Why no judge can run, or None when one can."""
    try:
        missing = missing_key_env_var(config.model, get_settings().llm)
    except ValueError as exc:
        return f"judge model {config.model!r} is not a valid <provider>/<model> id ({exc})"
    if missing:
        return f"the judge model's provider has no API key on the server (set {missing})"
    return None
