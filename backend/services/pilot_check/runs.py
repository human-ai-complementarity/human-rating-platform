"""Starting, executing and reading pilot check runs.

A run is one row in `pilot_check_runs`. The POST handler, or an admin write
that leaves the experiment ready to launch (`start_after_change`), inserts it
as `running` and hands the work to an asyncio task in the API process; the task
gathers what a rater would see, probes the assistance method, applies the
checks, and writes the result back, touching `updated_at` as it goes. A
`running` row that has not been touched for `STALE_AFTER` belongs to a
process that is gone: it reads as aborted and can be re-run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import PilotCheckSettings, get_settings
from database import Database
from models import Experiment, ExperimentStatus, PilotCheckRun, Upload
from schemas import PilotCheckFinding, PilotCheckProbe, PilotCheckRunResponse, PilotCheckStage
from services.assistance.base import exception_text
from services.assistance.registry import get_rater_instructions
from services.queries import fetch_experiment_or_404
from services.terms import terms_for_rater

from .assistance import probe_all
from .checks import CHECKS, CHECKS_BY_NAME, SEVERITY_RANK, CheckContext, CheckSpec, Finding
from .checks import severity_of
from .checks.base import merge_findings
from .judge import LLMJudge, judge_unavailable_reason
from .rater_view import build_views
from .sampling import sample_questions

logger = logging.getLogger(__name__)

# A running row not written to for this long belongs to a process that is
# gone. Every awaited step inside a run (a method start, a judge call) is
# bounded well under this, and progress is written after each one.
STALE_AFTER = timedelta(minutes=5)
ABORTED_MESSAGE = "The run stopped reporting progress; the server may have restarted."
# Strong references so the event loop does not drop a run mid-way.
_tasks: set[asyncio.Task] = set()

_STAGE_LABELS = {
    "sampling": "Sample questions",
    "assistance": "Probe assistance",
    "checks": "Check rendering",
    "done": "Result",
}


def pilot_settings() -> PilotCheckSettings:
    return get_settings().pilot_check


# --- Fingerprint ----------------------------------------------------------


def compute_fingerprint(
    experiment: Experiment,
    *,
    question_count: int,
    latest_upload_id: int | None,
    config_sha256: str,
) -> str:
    """Everything a run looks at that an admin can still change on a DRAFT.

    The consent text is deliberately not in here: reading it is a live call
    to the terms source, too slow for the experiment list. The flags that
    choose the statement are; a run stores the statement it saw in
    `terms_ref`. The pinned statement ids are not either: the first publish
    writes them, and a run made on the draft must stay valid for the rounds
    that follow (the consent check compares the pin to the flags on each run).
    """
    params = json.loads(experiment.assistance_params) if experiment.assistance_params else {}
    material = {
        "description": experiment.description,
        "system_prompt": experiment.system_prompt,
        "human_prompt_prefix": experiment.human_prompt_prefix,
        "human_prompt_suffix": experiment.human_prompt_suffix,
        "is_markdown": experiment.is_markdown,
        "render_math": experiment.render_math,
        "session_duration_minutes": experiment.session_duration_minutes,
        "study_flags": list(experiment.study_flags or []),
        "content_warning_details": experiment.content_warning_details,
        "assistance_method": experiment.assistance_method,
        "assistance_params": params,
        "latest_upload_id": latest_upload_id,
        "question_count": question_count,
        "config_sha256": config_sha256,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


async def fetch_latest_upload_ids(experiment_ids: list[int], db: AsyncSession) -> dict[int, int]:
    if not experiment_ids:
        return {}
    rows = await db.execute(
        select(Upload.experiment_id, func.max(Upload.id))
        .where(Upload.experiment_id.in_(experiment_ids))
        .group_by(Upload.experiment_id)
    )
    return {experiment_id: upload_id for experiment_id, upload_id in rows}


async def current_fingerprint(
    experiment: Experiment, *, question_count: int, db: AsyncSession
) -> str:
    uploads = await fetch_latest_upload_ids([experiment.id], db)
    return compute_fingerprint(
        experiment,
        question_count=question_count,
        latest_upload_id=uploads.get(experiment.id),
        config_sha256=pilot_settings().sha256(),
    )


# --- Reading runs ---------------------------------------------------------


async def latest_runs(experiment_ids: list[int], db: AsyncSession) -> dict[int, PilotCheckRun]:
    """The newest run per experiment, for the batch the list page shows."""
    if not experiment_ids:
        return {}
    newest = (
        select(PilotCheckRun.experiment_id, func.max(PilotCheckRun.id).label("id"))
        .where(PilotCheckRun.experiment_id.in_(experiment_ids))
        .group_by(PilotCheckRun.experiment_id)
        .subquery()
    )
    rows = (
        (await db.execute(select(PilotCheckRun).join(newest, PilotCheckRun.id == newest.c.id)))
        .scalars()
        .all()
    )
    return {run.experiment_id: run for run in rows}


async def latest_run(experiment_id: int, db: AsyncSession) -> PilotCheckRun | None:
    return (await latest_runs([experiment_id], db)).get(experiment_id)


async def _load_run(run_id: int, db: AsyncSession) -> PilotCheckRun | None:
    return (
        await db.execute(select(PilotCheckRun).where(PilotCheckRun.id == run_id))
    ).scalar_one_or_none()


def effective_status(run: PilotCheckRun, now: datetime) -> str:
    """`running` only while the task keeps touching the row."""
    if run.status == "running" and now - (run.updated_at or run.created_at) > STALE_AFTER:
        return "aborted"
    return run.status


def status_from(findings: list[Finding]) -> str:
    worst = max((SEVERITY_RANK[f.severity] for f in findings), default=-1)
    if worst >= SEVERITY_RANK["error"]:
        return "failed"
    if worst >= SEVERITY_RANK["warning"]:
        return "warnings"
    return "passed"


def plan_stages(experiment: Experiment, settings: PilotCheckSettings) -> list[PilotCheckStage]:
    """The stages a run of this experiment goes through, in order, for the
    card's stepper: sampling, assistance (assisted arms), the deterministic
    checks as a group, one stage per judged check that applies (when a judge
    is available), done."""
    assisted = experiment.assistance_method != "none"
    ids = ["sampling"] + (["assistance"] if assisted else []) + ["checks"]
    if not judge_unavailable_reason(settings.judge_model):
        ids += [c.name for c in CHECKS if c.judged and (assisted or not c.needs_assistance)]
    ids.append("done")
    return [PilotCheckStage(id=stage_id, label=_stage_label(stage_id)) for stage_id in ids]


def _stage_label(stage_id: str) -> str:
    if stage_id in CHECKS_BY_NAME:
        return f"Review {CHECKS_BY_NAME[stage_id].label.lower()}"
    return _STAGE_LABELS[stage_id]


def _finding_row(row: dict) -> dict:
    """A stored finding; rows written before findings named several questions
    carry one `question_id`."""
    if "question_ids" not in row:
        row = {
            **row,
            "question_ids": [] if row.get("question_id") is None else [row["question_id"]],
        }
        row.pop("question_id", None)
    return row


def build_run_response(
    run: PilotCheckRun, *, experiment: Experiment, current_fingerprint: str
) -> PilotCheckRunResponse:
    settings = pilot_settings()
    status = effective_status(run, datetime.now(UTC))
    steps = json.loads(run.assistance_steps) if run.assistance_steps else None
    findings = [
        PilotCheckFinding(
            **_finding_row(f),
            check_label=CHECKS_BY_NAME[f["check"]].label
            if f["check"] in CHECKS_BY_NAME
            else f["check"],
        )
        for f in json.loads(run.findings or "[]")
    ]
    return PilotCheckRunResponse(
        id=run.id,
        experiment_id=run.experiment_id,
        status=status,
        stage=run.stage,
        stage_detail=run.stage_detail if status == "running" else None,
        stages=plan_stages(experiment, settings),
        requested_by=run.requested_by,
        created_at=run.created_at,
        finished_at=run.finished_at,
        fingerprint=run.fingerprint,
        config_sha256=run.config_sha256,
        terms_ref=run.terms_ref,
        sample_question_ids=json.loads(run.sample_question_ids or "[]"),
        findings=findings,
        assistance_steps=[PilotCheckProbe(**s) for s in steps] if steps is not None else None,
        judge_model=run.judge_model,
        error=run.error if status != "aborted" or run.error else ABORTED_MESSAGE,
        stale=run.fingerprint != current_fingerprint,
        sample_size=settings.sample_size,
    )


# --- Starting and executing -----------------------------------------------


async def start_run(
    experiment: Experiment,
    *,
    requested_by: str | None,
    question_count: int,
    db: AsyncSession,
    database: Database,
) -> PilotCheckRun:
    if experiment.status == ExperimentStatus.FINISHED:
        raise HTTPException(status_code=400, detail="A finished experiment cannot be checked.")
    if question_count == 0:
        raise HTTPException(
            status_code=400, detail="Upload questions before running the pilot check."
        )
    now = datetime.now(UTC)
    current = await latest_run(experiment.id, db)
    if current is not None and current.status == "running":
        if effective_status(current, now) == "running":
            raise HTTPException(
                status_code=409, detail="A pilot check is already running for this experiment."
            )
        # Stopped reporting progress: the process that owned it is gone.
        current.status = "aborted"
        current.error = ABORTED_MESSAGE
        current.finished_at = now
    run = PilotCheckRun(
        experiment_id=experiment.id,
        status="running",
        stage="sampling",
        requested_by=requested_by,
        fingerprint=await current_fingerprint(experiment, question_count=question_count, db=db),
        config_sha256=pilot_settings().sha256(),
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    task = asyncio.create_task(execute_run(run.id, database), name=f"pilot-check-{run.id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run


def autostart_skip_reason(
    latest: PilotCheckRun | None, *, current_fingerprint: str, now: datetime
) -> str | None:
    """Why a change should not start a run: one is in progress, or the
    latest run already covers the experiment as it is. An aborted run covers
    nothing, whatever its fingerprint says."""
    if latest is None:
        return None
    status = effective_status(latest, now)
    if status == "running":
        return "a run is already in progress"
    if status != "aborted" and latest.fingerprint == current_fingerprint:
        return "the latest run is still fresh"
    return None


async def start_after_change(
    experiment_id: int, *, requested_by: str | None, db: AsyncSession, database: Database
) -> PilotCheckRun | None:
    """Start a run once an upload or an edit has landed, unless `auto_start`
    is off, the experiment is not yet ready to launch (`experiment_launch_blockers`:
    questions, instructions, a model with a key), it is FINISHED, or
    `autostart_skip_reason` says why not. Never raises: the write has
    succeeded and its response must say so."""
    attributes = {"experiment_id": experiment_id}
    try:
        if not pilot_settings().auto_start:
            return None
        experiment = await fetch_experiment_or_404(experiment_id, db)
        if experiment.status == ExperimentStatus.FINISHED:
            return None
        # Local: services.admin imports this module.
        from services.admin.queries import fetch_total_questions_for_experiment
        from services.admin.status import experiment_launch_blockers
        from services.admin.tags import fetch_tag_names_for_experiment

        question_count = await fetch_total_questions_for_experiment(experiment_id, db)
        blockers = experiment_launch_blockers(
            experiment,
            question_count=question_count,
            tags=await fetch_tag_names_for_experiment(experiment_id, db),
        )
        if blockers:
            logger.info(
                "pilot check not started: the experiment is missing %s",
                ", ".join(blockers),
                extra={"attributes": attributes},
            )
            return None
        skip = autostart_skip_reason(
            await latest_run(experiment_id, db),
            current_fingerprint=await current_fingerprint(
                experiment, question_count=question_count, db=db
            ),
            now=datetime.now(UTC),
        )
        if skip:
            logger.info("pilot check not started: %s", skip, extra={"attributes": attributes})
            return None
        run = await start_run(
            experiment,
            requested_by=requested_by,
            question_count=question_count,
            db=db,
            database=database,
        )
        logger.info(
            "pilot check started by a change",
            extra={"attributes": {**attributes, "run_id": run.id}},
        )
        return run
    except Exception:
        logger.exception("pilot check not started", extra={"attributes": attributes})
        return None


async def _start_follow_up(run_id: int, database: Database) -> None:
    """An edit that lands while a run is going is not checked by that run;
    once it finishes, start another if the experiment has moved on. Bounded:
    the next run's fingerprint matches, so it starts no third. Never raises:
    the run that just landed must not be marked aborted by this."""
    try:
        async with database.session() as db:
            run = await _load_run(run_id, db)
            if run is None or run.status == "aborted":
                return
            await start_after_change(
                run.experiment_id, requested_by=run.requested_by, db=db, database=database
            )
    except Exception:
        logger.exception(
            "pilot check follow-up not started", extra={"attributes": {"run_id": run_id}}
        )


async def cancel_runs() -> None:
    """Stop the runs still going at shutdown; each marks its row aborted."""
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def execute_run(run_id: int, database: Database) -> None:
    try:
        await _perform(run_id, database, pilot_settings())
        await _start_follow_up(run_id, database)
    except asyncio.CancelledError:
        await _finish(run_id, database, status="aborted", error="The server stopped mid-run.")
        raise
    except Exception as exc:  # the row must say why, not a log line alone
        logger.exception("pilot check run failed", extra={"attributes": {"run_id": run_id}})
        await _finish(run_id, database, status="aborted", error=exception_text(exc))


async def _gather(
    run_id: int, database: Database, settings: PilotCheckSettings
) -> CheckContext | None:
    """Everything the checks read, loaded in one session."""
    async with database.session() as db:
        run = await _load_run(run_id, db)
        if run is None:
            return None
        experiment = await fetch_experiment_or_404(run.experiment_id, db)
        from services.admin.queries import fetch_total_questions_for_experiment

        question_count = await fetch_total_questions_for_experiment(experiment.id, db)
        questions = await sample_questions(experiment.id, size=settings.sample_size, db=db)
        ctx = CheckContext(
            experiment=experiment,
            settings=settings,
            question_count=question_count,
            views=await build_views(experiment, questions, db),
            assistance_instructions=get_rater_instructions(experiment.assistance_method) or None,
        )
        try:
            ctx.terms = await terms_for_rater(experiment, db)
        except HTTPException as exc:
            ctx.terms_error = str(exc.detail)
        except Exception as exc:
            ctx.terms_error = exception_text(exc)
        return ctx


async def _set_stage(
    run_id: int, database: Database, stage: str, detail: str | None = None
) -> None:
    """Record progress on the run row; the card polls it. Never fatal."""
    try:
        async with database.session() as db:
            run = await _load_run(run_id, db)
            if run is None:
                return
            run.stage = stage
            run.stage_detail = detail
            run.updated_at = datetime.now(UTC)
            await db.commit()
    except Exception:
        logger.exception("pilot check stage not saved", extra={"attributes": {"run_id": run_id}})


async def _run_check(run_id: int, ctx: CheckContext, check: CheckSpec) -> list[Finding]:
    try:
        return await check.run(ctx, severity_of(ctx.settings, check))
    except Exception as exc:
        logger.exception(
            "pilot check crashed", extra={"attributes": {"run_id": run_id, "check": check.name}}
        )
        # A warning, not info: a check that never ran must not read as passed.
        return [
            Finding(
                check.name,
                "warning",
                "The check itself crashed, so it did not run.",
                detail=exception_text(exc),
            )
        ]


async def _perform(run_id: int, database: Database, settings: PilotCheckSettings) -> None:
    ctx = await _gather(run_id, database, settings)
    if ctx is None:
        return  # deleted while starting
    experiment = ctx.experiment

    if experiment.assistance_method != "none":
        total = len(ctx.views)

        async def probed(done: int, of: int) -> None:
            await _set_stage(run_id, database, "assistance", f"{done} of {of} questions probed")

        await _set_stage(run_id, database, "assistance", f"0 of {total} questions probed")
        ctx.probes = await probe_all(experiment, ctx.views, on_progress=probed)

    unavailable = judge_unavailable_reason(settings.judge_model)
    ctx.judge_unavailable_reason = unavailable
    ctx.judge = None if unavailable else LLMJudge(settings.judge_model)

    # Deterministic checks first, then the judged ones: the judge calls are
    # the slow part, and the stepper shows each as its own stage with the
    # check reporting how far through the sample it is.
    findings: list[Finding] = []
    await _set_stage(run_id, database, "checks")
    for check in CHECKS:
        if not check.judged:
            findings.extend(merge_findings(await _run_check(run_id, ctx, check)))
    for check in CHECKS:
        if not check.judged:
            continue
        if ctx.judge is not None:
            await _set_stage(run_id, database, check.name)

            async def report(text: str, _stage: str = check.name) -> None:
                await _set_stage(run_id, database, _stage, text)

            ctx.report = report
        findings.extend(merge_findings(await _run_check(run_id, ctx, check)))
    ctx.report = None

    terms = ctx.terms
    await _finish(
        run_id,
        database,
        status=status_from(findings),
        findings=findings,
        probes=ctx.probes,
        sample_ids=[view.question_id for view in ctx.views],
        terms_ref=f"{terms.consent.ref}:{terms.consent.sha256}" if terms else None,
        judge_model=ctx.judge.model if ctx.judge else None,
    )


async def _finish(
    run_id: int,
    database: Database,
    *,
    status: str,
    error: str | None = None,
    findings: list[Finding] | None = None,
    probes=None,
    sample_ids: list[int] | None = None,
    terms_ref: str | None = None,
    judge_model: str | None = None,
) -> None:
    try:
        async with database.session() as db:
            run = await _load_run(run_id, db)
            if run is None:
                return  # experiment deleted mid-run; the cascade took the row
            now = datetime.now(UTC)
            run.status = status
            run.error = error
            run.finished_at = now
            run.updated_at = now
            # An aborted run keeps the stage it died in; a finished one is done.
            if status != "aborted":
                run.stage = "done"
            run.stage_detail = None
            if findings is not None:
                run.findings = json.dumps([f.to_dict() for f in findings])
            if probes is not None:
                run.assistance_steps = json.dumps([p.to_dict() for p in probes])
            if sample_ids is not None:
                run.sample_question_ids = json.dumps(sample_ids)
            run.terms_ref = terms_ref
            run.judge_model = judge_model
            await db.commit()
    except Exception:
        logger.exception("pilot check result not saved", extra={"attributes": {"run_id": run_id}})
