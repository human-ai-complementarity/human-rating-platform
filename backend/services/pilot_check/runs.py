"""Starting, executing, summarising and gating on pilot check runs.

A run is one row in `pilot_check_runs`. The POST handler inserts it as
`running` and hands the work to an asyncio task in the API process; the task
gathers what a rater would see, probes the assistance method, applies the
checks, and writes the result back. A `running` row older than the run
deadline is reported as aborted (the process restarted) and can be re-run.

The summary on `ExperimentResponse` and the launch gate both come from
`summarise`, so the UI and the 409 cannot disagree about why a launch needs
acknowledging.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database import Database
from models import Experiment, ExperimentStatus, PilotCheckRun, Upload
from schemas import PilotCheckFinding, PilotCheckProbe, PilotCheckRunResponse, PilotCheckSummary
from services.assistance.base import exception_text
from services.assistance.registry import get_rater_instructions
from services.queries import fetch_experiment_or_404
from services.terms import RaterTerms, terms_for_rater

from .assistance import AssistanceProbe, probe_all
from .checks.base import CheckContext, Finding
from .checks.registry import get_check
from .config import SEVERITY_RANK, PilotCheckConfig, get_pilot_check_config
from .judge import LLMJudge, judge_unavailable_reason
from .rater_view import build_views
from .sampling import sample_questions

logger = logging.getLogger(__name__)

RUN_DEADLINE_GRACE_SECONDS = 60
# Strong references so the event loop does not drop a run mid-way.
_tasks: set[asyncio.Task] = set()


def run_deadline_seconds(config: PilotCheckConfig) -> float:
    """An upper bound on a run: every probe at its timeout, two judge calls
    per question at the provider timeout with retries, plus grace."""
    llm = get_settings().llm
    size = config.sample.size
    judge_calls = 2 * size + 1
    return (
        size * config.assistance.per_question_timeout_seconds
        + judge_calls * llm.request_timeout * (llm.max_retries + 1)
        + RUN_DEADLINE_GRACE_SECONDS
    )


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
    to the terms source, too slow for the experiment list. The flags and pins
    that choose the statement are; a run stores the statement it saw in
    `terms_ref`.
    """
    params = json.loads(experiment.assistance_params) if experiment.assistance_params else {}
    material = {
        "description": experiment.description,
        "system_prompt": experiment.system_prompt,
        "human_prompt_prefix": experiment.human_prompt_prefix,
        "human_prompt_suffix": experiment.human_prompt_suffix,
        "is_markdown": experiment.is_markdown,
        "session_duration_minutes": experiment.session_duration_minutes,
        "study_flags": list(experiment.study_flags or []),
        "content_warning_details": experiment.content_warning_details,
        "consent_statement_id": experiment.consent_statement_id,
        "debrief_statement_id": experiment.debrief_statement_id,
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


async def get_run(run_id: int, db: AsyncSession) -> PilotCheckRun:
    run = (
        await db.execute(select(PilotCheckRun).where(PilotCheckRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Pilot check run not found")
    return run


def _counts(findings: list[dict]) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for finding in findings:
        counts[finding.get("severity", "info")] = counts.get(finding.get("severity", "info"), 0) + 1
    return counts


def _status_from(findings: list[Finding]) -> str:
    worst = max((SEVERITY_RANK[f.severity] for f in findings), default=-1)
    if worst >= SEVERITY_RANK["error"]:
        return "failed"
    if worst >= SEVERITY_RANK["warning"]:
        return "warnings"
    return "passed"


def _effective_status(run: PilotCheckRun, config: PilotCheckConfig, now: datetime) -> str:
    if run.status == "running":
        age = now - run.created_at
        if age > timedelta(seconds=run_deadline_seconds(config)):
            return "aborted"
    return run.status


def summarise(
    run: PilotCheckRun | None,
    *,
    current_fingerprint: str | None,
    config: PilotCheckConfig,
    now: datetime | None = None,
) -> PilotCheckSummary:
    """The state the experiment page shows and the launch gate reads."""
    now = now or datetime.now(UTC)
    mode = config.gate.mode
    if run is None:
        return PilotCheckSummary(
            state="not_run",
            gate_mode=mode,
            needs_acknowledgement=mode != "off",
            reason="The pilot check has not been run.",
        )
    status = _effective_status(run, config, now)
    counts = _counts(json.loads(run.findings or "[]"))
    if status == "running":
        state, reason = "running", "The pilot check is still running."
    elif status == "aborted":
        state, reason = "aborted", "The last pilot check did not finish."
    elif current_fingerprint is not None and run.fingerprint != current_fingerprint:
        state, reason = "stale", "The experiment changed since the last pilot check."
    else:
        state = status
        reason = (
            None
            if status == "passed"
            else (
                f"The last pilot check found {counts['error']} error(s) and "
                f"{counts['warning']} warning(s)."
            )
        )
    gate_rank = SEVERITY_RANK[config.gate.severity]
    flagged = sum(count for level, count in counts.items() if SEVERITY_RANK[level] >= gate_rank)
    needs_ack = mode != "off" and (state not in ("passed", "warnings") or flagged > 0)
    if state == "warnings" and flagged == 0:
        reason = None
    return PilotCheckSummary(
        state=state,
        run_id=run.id,
        created_at=run.created_at,
        finished_at=run.finished_at,
        counts=counts,
        gate_mode=mode,
        needs_acknowledgement=needs_ack,
        reason=reason,
    )


async def summary_for(
    experiment: Experiment, *, question_count: int, db: AsyncSession
) -> PilotCheckSummary:
    """One experiment's summary; the list uses `summaries_for` instead."""
    return (await summaries_for([(experiment, question_count)], db))[experiment.id]


async def summaries_for(
    rows: list[tuple[Experiment, int]], db: AsyncSession
) -> dict[int, PilotCheckSummary]:
    config = get_pilot_check_config()
    ids = [experiment.id for experiment, _ in rows]
    runs = await latest_runs(ids, db)
    uploads = await fetch_latest_upload_ids(ids, db)
    now = datetime.now(UTC)
    return {
        experiment.id: summarise(
            runs.get(experiment.id),
            current_fingerprint=compute_fingerprint(
                experiment,
                question_count=question_count,
                latest_upload_id=uploads.get(experiment.id),
                config_sha256=config.sha256,
            ),
            config=config,
            now=now,
        )
        for experiment, question_count in rows
    }


def build_run_response(
    run: PilotCheckRun, *, current_fingerprint: str | None, config: PilotCheckConfig
) -> PilotCheckRunResponse:
    now = datetime.now(UTC)
    status = _effective_status(run, config, now)
    steps = json.loads(run.assistance_steps) if run.assistance_steps else None
    return PilotCheckRunResponse(
        id=run.id,
        experiment_id=run.experiment_id,
        status=status,
        stage=run.stage,
        stage_detail=run.stage_detail,
        stages=json.loads(run.stages or "[]"),
        requested_by=run.requested_by,
        created_at=run.created_at,
        finished_at=run.finished_at,
        fingerprint=run.fingerprint,
        config_sha256=run.config_sha256,
        terms_ref=run.terms_ref,
        sample_question_ids=json.loads(run.sample_question_ids or "[]"),
        findings=[PilotCheckFinding(**f) for f in json.loads(run.findings or "[]")],
        assistance_steps=[PilotCheckProbe(**s) for s in steps] if steps is not None else None,
        judge_model=run.judge_model,
        error=run.error
        if status != "aborted" or run.error
        else "The run did not finish before its deadline; the server may have restarted.",
        stale=current_fingerprint is not None and run.fingerprint != current_fingerprint,
        enabled_checks=config.enabled_checks(),
        sample_size=config.sample.size,
    )


# --- Launch gate ----------------------------------------------------------


@dataclass(frozen=True)
class PilotCheckGate:
    """What a launch records about the check it was made against."""

    summary: PilotCheckSummary
    # Set when the admin launched past a check the gate would have stopped.
    acknowledged_by: str | None

    @property
    def run_id(self) -> int | None:
        return self.summary.run_id


async def gate_for(
    experiment: Experiment,
    db: AsyncSession,
    *,
    acknowledged: bool,
    admin_email: str | None,
    question_count: int,
) -> PilotCheckGate:
    """Refuse a study creation the pilot check should stop (docs/pilot-check.md).

    Runs after `assert_launch_ready`: hard blockers stay hard. With the gate in
    "warn" mode a missing, stale, running or failing check is a 409 carrying the
    summary, unless the request acknowledges it; "block" refuses regardless;
    "off" records nothing.
    """
    summary = await summary_for(experiment, question_count=question_count, db=db)
    if not summary.needs_acknowledgement:
        return PilotCheckGate(summary=summary, acknowledged_by=None)
    if summary.gate_mode == "block":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pilot_check_blocked",
                "message": f"{summary.reason} Run the pilot check and fix what it finds "
                "before creating a study.",
                "pilot_check": summary.model_dump(mode="json"),
            },
        )
    if not acknowledged:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pilot_check",
                "message": f"{summary.reason} Run the pilot check first, or acknowledge "
                "and launch anyway.",
                "pilot_check": summary.model_dump(mode="json"),
            },
        )
    logger.warning(
        "Launching past the pilot check",
        extra={
            "attributes": {
                "experiment_id": experiment.id,
                "pilot_check_state": summary.state,
                "acknowledged_by": admin_email,
            }
        },
    )
    return PilotCheckGate(summary=summary, acknowledged_by=admin_email or "unknown")


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
    config = get_pilot_check_config()
    now = datetime.now(UTC)
    current = await latest_run(experiment.id, db)
    if current is not None and current.status == "running":
        if _effective_status(current, config, now) == "running":
            raise HTTPException(
                status_code=409, detail="A pilot check is already running for this experiment."
            )
        # Past its deadline: the process that owned it is gone.
        current.status = "aborted"
        current.error = "The run did not finish before its deadline; the server may have restarted."
        current.finished_at = now
    uploads = await fetch_latest_upload_ids([experiment.id], db)
    run = PilotCheckRun(
        experiment_id=experiment.id,
        status="running",
        stage="sampling",
        stages=json.dumps(plan_stages(experiment, config)),
        requested_by=requested_by,
        fingerprint=compute_fingerprint(
            experiment,
            question_count=question_count,
            latest_upload_id=uploads.get(experiment.id),
            config_sha256=config.sha256,
        ),
        config_sha256=config.sha256,
        created_at=now,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    task = asyncio.create_task(execute_run(run.id, database), name=f"pilot-check-{run.id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run


async def execute_run(run_id: int, database: Database) -> None:
    config = get_pilot_check_config()
    try:
        async with asyncio.timeout(run_deadline_seconds(config)):
            await _perform(run_id, database, config)
    except TimeoutError:
        await _finish(run_id, database, status="aborted", error="The run hit its deadline.")
    except Exception as exc:  # the row must say why, not a log line alone
        logger.exception("pilot check run failed", extra={"attributes": {"run_id": run_id}})
        await _finish(run_id, database, status="aborted", error=exception_text(exc))


@dataclass
class _Gathered:
    experiment: Experiment
    question_count: int
    views: list
    sample_ids: list[int]
    terms: RaterTerms | None
    terms_error: str | None


async def _gather(run_id: int, database: Database, config: PilotCheckConfig) -> _Gathered | None:
    async with database.session() as db:
        run = (
            await db.execute(select(PilotCheckRun).where(PilotCheckRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            return None
        experiment = await fetch_experiment_or_404(run.experiment_id, db)
        from services.admin.queries import fetch_total_questions_for_experiment

        question_count = await fetch_total_questions_for_experiment(experiment.id, db)
        questions = await sample_questions(
            experiment.id, size=config.sample.size, seed=config.sample.seed, db=db
        )
        views = await build_views(experiment, questions, db)
        terms: RaterTerms | None = None
        terms_error: str | None = None
        try:
            terms = await terms_for_rater(experiment, db)
        except HTTPException as exc:
            terms_error = str(exc.detail)
        except Exception as exc:
            terms_error = exception_text(exc)
        return _Gathered(
            experiment=experiment,
            question_count=question_count,
            views=views,
            sample_ids=[view.question_id for view in views],
            terms=terms,
            terms_error=terms_error,
        )


def plan_stages(experiment: Experiment, config: PilotCheckConfig) -> list[str]:
    """The stages this run will go through, in order, for the UI's stepper:
    sampling, assistance (assisted arms), checks (the deterministic ones as a
    group), one stage per judged check that applies (when a judge is
    available), done."""
    enabled = config.enabled_checks()
    assisted = experiment.assistance_method != "none"
    stages = ["sampling"]
    if assisted:
        stages.append("assistance")
    if any(not get_check(name).judged for name in enabled):
        stages.append("checks")
    if not judge_unavailable_reason(config.judge):
        for name in enabled:
            check = get_check(name)
            if check.judged and (assisted or not check.needs_assistance):
                stages.append(name)
    stages.append("done")
    return stages


async def _set_stage(
    run_id: int, database: Database, stage: str, detail: str | None = None
) -> None:
    """Record progress on the run row; the card polls it. Never fatal."""
    try:
        async with database.session() as db:
            run = (
                await db.execute(select(PilotCheckRun).where(PilotCheckRun.id == run_id))
            ).scalar_one_or_none()
            if run is None:
                return
            run.stage = stage
            run.stage_detail = detail
            await db.commit()
    except Exception:
        logger.exception("pilot check stage not saved", extra={"attributes": {"run_id": run_id}})


async def _run_checks(
    run_id: int, ctx: CheckContext, config: PilotCheckConfig, names: list[str]
) -> list[Finding]:
    findings: list[Finding] = []
    for name in names:
        check = get_check(name)
        try:
            findings.extend(await check.run(ctx, config.severity_of(name)))
        except Exception as exc:
            logger.exception(
                "pilot check crashed", extra={"attributes": {"run_id": run_id, "check": name}}
            )
            findings.append(
                Finding(
                    check=name,
                    severity="info",
                    message="Skipped: the check itself crashed.",
                    detail=exception_text(exc),
                )
            )
    return findings


async def _perform(run_id: int, database: Database, config: PilotCheckConfig) -> None:
    gathered = await _gather(run_id, database, config)
    if gathered is None:
        return  # deleted while starting
    experiment = gathered.experiment
    terms = gathered.terms
    terms_error = gathered.terms_error

    probes: list[AssistanceProbe] | None = None
    method_params: dict = {}
    if experiment.assistance_method != "none":
        total = len(gathered.views)
        await _set_stage(run_id, database, "assistance", f"0 of {total} questions probed")

        async def progress(done: int, of: int) -> None:
            await _set_stage(run_id, database, "assistance", f"{done} of {of} questions probed")

        try:
            probes, method_params = await probe_all(
                experiment,
                gathered.views,
                timeout_seconds=config.assistance.per_question_timeout_seconds,
                on_progress=progress,
            )
        except Exception as exc:
            # The method could not even be resolved: every question fails alike.
            detail = exception_text(exc)
            probes = [
                AssistanceProbe(
                    question_id=view.question_id,
                    step_type="none",
                    payload={},
                    failure_reason="execution_error",
                    failure_detail=detail,
                    latency_ms=0,
                )
                for view in gathered.views
            ]

    unavailable = judge_unavailable_reason(config.judge)
    judge = None if unavailable else LLMJudge(config.judge)

    ctx = CheckContext(
        experiment=experiment,
        config=config,
        question_count=gathered.question_count,
        views=gathered.views,
        terms=terms,
        terms_error=terms_error,
        probes=probes,
        judge=judge,
        judge_unavailable_reason=unavailable,
        assistance_instructions=get_rater_instructions(experiment.assistance_method) or None,
        method_params=method_params,
    )
    # Deterministic checks first, then the judged ones: the judge calls are
    # the slow part, and the stepper shows them as their own stage.
    enabled = config.enabled_checks()
    deterministic = [name for name in enabled if not get_check(name).judged]
    judged_names = [name for name in enabled if get_check(name).judged]
    findings: list[Finding] = []
    if deterministic:
        await _set_stage(run_id, database, "checks", f"{len(deterministic)} checks")
        findings.extend(await _run_checks(run_id, ctx, config, deterministic))
    # Each judged check is its own stage, with the check reporting how far
    # through the sample it is. Without a judge they only emit a skip note.
    for name in judged_names:
        if judge is not None:
            await _set_stage(run_id, database, name)

            async def progress(text: str, _name: str = name) -> None:
                await _set_stage(run_id, database, _name, text)

            ctx.progress = progress
        findings.extend(await _run_checks(run_id, ctx, config, [name]))
    ctx.progress = None

    await _finish(
        run_id,
        database,
        status=_status_from(findings),
        findings=findings,
        probes=probes,
        sample_ids=gathered.sample_ids,
        terms_ref=f"{terms.consent.ref}:{terms.consent.sha256}" if terms else None,
        judge_model=judge.model if judge else None,
    )


async def _finish(
    run_id: int,
    database: Database,
    *,
    status: str,
    error: str | None = None,
    findings: list[Finding] | None = None,
    probes: list[AssistanceProbe] | None = None,
    sample_ids: list[int] | None = None,
    terms_ref: str | None = None,
    judge_model: str | None = None,
) -> None:
    try:
        async with database.session() as db:
            run = (
                await db.execute(select(PilotCheckRun).where(PilotCheckRun.id == run_id))
            ).scalar_one_or_none()
            if run is None:
                return  # experiment deleted mid-run; the cascade took the row
            run.status = status
            run.error = error
            run.finished_at = datetime.now(UTC)
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
