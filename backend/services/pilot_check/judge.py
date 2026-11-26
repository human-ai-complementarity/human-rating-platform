"""The LLM judge behind the judged checks.

One call shape for every judged check: a task (the rubric), the material
(what the rater sees), and a JSON verdict back. Rubrics live with their
checks so a prompt edit is a reviewed change with a test; this module only
knows how to ask.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from config import get_settings
from services.assistance.llm import complete, missing_key_env_var
from services.assistance.methods.top_n import strip_markdown_json

_SYSTEM_PROMPT = """\
You review the material a paid crowd rater is about to be shown on a rating \
platform, before the study goes live. You are not answering the task; you are \
checking that a careful rater could. Be concrete and brief: name the problem \
and, where you can, quote the words that cause it. Do not flag matters of \
taste. Reply with JSON only."""


class JudgeError(RuntimeError):
    """The judge could not be asked or did not answer in the shape asked for."""


@dataclass
class Verdict:
    ok: bool
    issues: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class Problem:
    """One distinct problem across several questions, from `Judge.group`."""

    summary: str
    question_ids: list[int]
    issues: list[str] = field(default_factory=list)


class Judge(Protocol):
    model: str

    async def __call__(self, *, task: str, material: str) -> Verdict: ...

    async def group(self, *, task: str, material: str) -> list[Problem]: ...


def _json_object(raw: str) -> dict:
    text = strip_markdown_json(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge reply was not JSON: {raw[:200]!r}") from exc
    if not isinstance(data, dict):
        raise JudgeError(f"judge reply was not a JSON object: {raw[:200]!r}")
    return data


def parse_verdict(raw: str) -> Verdict:
    data = _json_object(raw)
    if not isinstance(data.get("ok"), bool):
        raise JudgeError(f"judge reply missing a boolean 'ok': {raw[:200]!r}")
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    return Verdict(
        ok=data["ok"],
        issues=[str(issue).strip() for issue in issues if str(issue).strip()],
        summary=str(data.get("summary") or "").strip(),
    )


def parse_problems(raw: str) -> list[Problem]:
    items = _json_object(raw).get("problems")
    if not isinstance(items, list):
        raise JudgeError(f"judge reply missing a 'problems' list: {raw[:200]!r}")
    problems = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ids = item.get("question_ids") or []
        issues = item.get("issues") or []
        problems.append(
            Problem(
                summary=str(item.get("summary") or "").strip(),
                question_ids=[int(q) for q in ids if str(q).strip().lstrip("-").isdigit()],
                issues=[str(i).strip() for i in issues if str(i).strip()],
            )
        )
    return problems


_VERDICT_SHAPE = (
    'Answer with JSON: {"ok": true|false, "issues": ["..."], "summary": "..."}. '
    "ok is false only when a rater would be confused, blocked or misled."
)
_PROBLEMS_SHAPE = (
    'Answer with JSON: {"problems": [{"summary": "...", "question_ids": [1, 2], '
    '"issues": ["..."]}]}. summary is one sentence naming the problem; issues are '
    "the specifics, each one sentence."
)


class LLMJudge:
    def __init__(self, model: str):
        self.model = model

    async def __call__(self, *, task: str, material: str) -> Verdict:
        return parse_verdict(await self._ask(task, material, _VERDICT_SHAPE))

    async def group(self, *, task: str, material: str) -> list[Problem]:
        return parse_problems(await self._ask(task, material, _PROBLEMS_SHAPE))

    async def _ask(self, task: str, material: str, shape: str) -> str:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"{task}\n\n=== Material ===\n{material}\n=== End of material ===\n\n{shape}"
                ),
            },
        ]
        try:
            return await complete(
                messages,
                settings=get_settings().llm,
                model=self.model,
                response_format={"type": "json_object"},
                # OpenAI's reasoning models reject a non-default temperature.
                temperature=None if self.model.startswith("oai/") else 0.0,
            )
        except Exception as exc:
            raise JudgeError(f"{type(exc).__name__}: {exc}") from exc


def judge_unavailable_reason(model: str) -> str | None:
    """Why no judge can run, or None when one can."""
    try:
        missing = missing_key_env_var(model, get_settings().llm)
    except ValueError as exc:
        return f"judge model {model!r} is not a valid <provider>/<model> id ({exc})"
    if missing:
        return f"the judge model's provider has no API key on the server (set {missing})"
    return None
