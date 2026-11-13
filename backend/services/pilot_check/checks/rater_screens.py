"""Does each screen a rater meets look right, read from a screenshot.

The other checks read the strings a rater is sent; this one reads the page
those strings become in a browser: the consent screen, the intro, and each
sampled question with its assistance. A vision judge reads every screenshot;
a flagged one becomes a finding the admin can open the screenshot from.
"""

from __future__ import annotations

from .. import screens as browser
from ..judge import JudgeError
from .base import (
    CheckContext,
    CheckSpec,
    Finding,
    Severity,
    grouped_findings,
    judge_failed,
    no_judge,
    skipped,
    verdict_finding,
)

NAME = "rater_screens"

_TASK = """\
This is a screenshot of one screen of a rating study, exactly as a paid \
crowd rater sees it in their browser ({what}). The "Preview mode" banner \
at the top is shown only to the researcher; ignore it. Nothing has been \
answered yet, so a disabled submit or agree button and an unticked box are \
expected. Flag what would confuse, block or mislead the rater: Markdown or \
LaTeX showing as raw symbols, text cut off, overlapping or unreadable, an \
image that failed to load, an empty area where content should be, an error \
message, assistance that failed or says nothing useful, answer options that \
are missing or do not fit the question, placeholder or template text. Do not \
flag layout taste."""

_WHAT = {
    browser.CONSENT: "the consent form, shown before anything else",
    browser.INTRO: "the instructions screen, shown once after consent",
}
_QUESTION = "a question to answer, with any AI assistance beside it"

# As `judge_each`: a dead judge fails every call alike.
_MAX_JUDGE_FAILURES_IN_A_ROW = 3


async def run(ctx: CheckContext, severity: Severity) -> list[Finding]:
    if not ctx.settings.rater_app_url:
        return [skipped(NAME, "no rater app URL is set (pilot_check.rater_app_url)")]
    if ctx.judge is None:
        return [no_judge(NAME, ctx)]
    question_ids = [view.question_id for view in ctx.views]
    # Named per screen rather than "n of total": whether an intro screen
    # exists is only known once the browser gets past consent.
    position = {qid: index for index, qid in enumerate(question_ids, start=1)}

    async def captured(shot: browser.Screen) -> None:
        if shot.question_id is None:
            name = "the consent form" if shot.key == browser.CONSENT else "the intro"
            await ctx.note(f"captured {name}")
        else:
            await ctx.note(f"captured question {position[shot.question_id]} of {len(question_ids)}")

    await ctx.note("opening the rater page")
    try:
        shots = await browser.capture(
            app_url=ctx.settings.rater_app_url,
            experiment_id=ctx.experiment.id,
            run_id=ctx.run_id,
            question_ids=question_ids,
            on_progress=captured,
        )
    except browser.BrowserUnavailable as exc:
        return [
            Finding(
                NAME,
                "warning",
                "The server could not open a browser, so no screens were captured.",
                detail=str(exc),
            )
        ]

    findings: list[Finding] = []
    flagged_questions = []
    in_a_row = 0
    for index, shot in enumerate(shots, start=1):
        ctx.screenshots[shot.key] = shot.png
        ids = [shot.question_id] if shot.question_id is not None else []
        for problem in shot.problems:
            findings.append(Finding(NAME, severity, problem, question_ids=ids, screens=[shot.key]))
        what = _WHAT.get(shot.key, _QUESTION)
        try:
            verdict = await ctx.judge.look(task=_TASK.format(what=what), png=shot.png)
        except JudgeError as exc:
            failure = judge_failed(NAME, exc, question_ids=ids)
            failure.screens = [shot.key]
            findings.append(failure)
            in_a_row += 1
            if in_a_row >= _MAX_JUDGE_FAILURES_IN_A_ROW and index < len(shots):
                findings.append(
                    Finding(
                        NAME,
                        "warning",
                        f"The judge failed {in_a_row} times in a row, so the remaining "
                        f"{len(shots) - index} screens were not reviewed.",
                    )
                )
                break
            continue
        in_a_row = 0
        await ctx.note(f"{index} of {len(shots)} screens reviewed")
        if verdict.ok:
            continue
        if shot.question_id is not None:
            flagged_questions.append((shot.question_id, verdict))
        else:
            finding = verdict_finding(NAME, verdict, severity)
            if finding is not None:
                finding.screens = [shot.key]
                findings.append(finding)

    # One finding per distinct problem across the questions, as the other
    # per-question checks do, each linking every screenshot it came from.
    for finding in await grouped_findings(NAME, ctx, severity, flagged_questions):
        finding.screens = [browser.question_screen(qid) for qid in finding.question_ids]
        findings.append(finding)
    return findings


CHECK = CheckSpec(NAME, "Rater screens", run, "warning", judged=True, needs_browser=True)
