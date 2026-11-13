"""Unit tests for the pilot check (docs/pilot-check.md): the sample, the
deterministic checks against hand-built contexts, the judge's reply parsing,
and the summary the gate and the UI read."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from config import PilotCheckSettings
from models import Experiment, ExperimentStatus, PilotCheckRun, Question
from schemas import QuestionResponse
from services.assistance.base import InteractionStep, StepType
from services.assistance.events import _MethodCall
from services.assistance.llm import speculative_call
from services.pilot_check import assistance as probe_runner
from services.pilot_check.assistance import AssistanceProbe
from services.pilot_check.checks import (
    CHECKS,
    CHECKS_BY_NAME,
    CheckContext,
    CheckSpec,
    assistance_health,
    assistance_output,
    consent,
    instructions,
    question_coherent,
    markdown_mode,
    question_renders,
    rater_screens,
    validate_severities,
)
from services.pilot_check.checks.base import Finding, merge_findings
from services.pilot_check.summary import summarise
from services.pilot_check.judge import JudgeError, Problem, Verdict, parse_problems, parse_verdict
from services.pilot_check.rater_view import RaterQuestionView
from services.pilot_check import screens as browser
from services.pilot_check.runs import (
    QUEUE_TIMEOUT,
    _run_check,
    autostart_skip_reason,
    compute_fingerprint,
    effective_status,
    plan_stages,
)
from services.pilot_check.sampling import choose_sample
from services.terms.service import PLACEHOLDER_CONSENT, RaterTerms, StatementText


@pytest.fixture(scope="module")
def config() -> PilotCheckSettings:
    return PilotCheckSettings()


def _experiment(**overrides) -> Experiment:
    values = {
        "id": 7,
        "name": "Passage rating",
        "description": "Rate each passage for accuracy.",
        "internal_name": "passages fall26 control",
        "assistance_method": "none",
        "is_markdown": False,
        "status": ExperimentStatus.DRAFT,
    }
    values.update(overrides)
    return Experiment(**values)


def _view(
    experiment: Experiment,
    *,
    qid: int = 1,
    text: str = "Is the passage accurate?",
    options: str | None = "Yes|No",
    question_type: str = "MC",
    parent_id: int | None = None,
    parent_text: str | None = None,
) -> RaterQuestionView:
    question = Question(
        id=qid,
        experiment_id=experiment.id,
        question_id=f"q{qid}",
        question_text=text,
        options=options,
        question_type=question_type,
        parent_question_id=parent_id,
    )
    # Built directly rather than through the async rater mapper, which these
    # sync and async tests both call; the e2e runs go through the mapper.
    response = QuestionResponse(
        id=question.id,
        question_text=question.question_text,
        options=question.options,
        question_type=question.question_type,
        parent_question_text=parent_text,
        is_markdown=experiment.is_markdown,
        render_math=experiment.render_math,
    )
    return RaterQuestionView(
        response=response,
        human_prompt_prefix=experiment.human_prompt_prefix,
        human_prompt_suffix=experiment.human_prompt_suffix,
        question=question,
    )


def _ctx(config, experiment: Experiment, views=None, **kwargs) -> CheckContext:
    return CheckContext(
        experiment=experiment,
        settings=config,
        question_count=kwargs.pop("question_count", 10),
        views=views if views is not None else [_view(experiment)],
        **kwargs,
    )


def _probe(qid: int, step_type: str = "ask_input", payload: dict | None = None, **kw):
    return AssistanceProbe(
        question_id=qid,
        step_type=step_type,
        payload=payload or {},
        failure_reason=kw.get("failure_reason"),
        failure_detail=kw.get("failure_detail"),
        latency_ms=kw.get("latency_ms", 1200),
    )


def _severities(findings) -> list[str]:
    return [f.severity for f in findings]


# --- Checks and settings --------------------------------------------------


def test_every_check_has_a_unique_name_and_a_label(config):
    assert len(CHECKS_BY_NAME) == len(CHECKS)
    assert all(check.label for check in CHECKS)
    assert config.sha256() != PilotCheckSettings(sample_size=3).sha256()
    # The gate mode is not part of what a run looked at.
    assert config.sha256() == PilotCheckSettings(gate_mode="off").sha256()
    # When a run starts is not part of what it looked at.
    assert config.sha256() == PilotCheckSettings(auto_start=False).sha256()
    # Nor where it runs.
    worker = PilotCheckSettings(runner="worker", worker_concurrency=3)
    assert config.sha256() == worker.sha256()


# --- Sampling -------------------------------------------------------------


def test_the_sample_covers_every_type_and_a_sub_question():
    rows = [(i, "MC", False) for i in range(1, 40)] + [(40, "free", False), (41, "MC", True)]
    chosen = choose_sample(rows, 5, seed=1)
    assert len(chosen) == 5
    assert 40 in chosen and 41 in chosen
    assert chosen == sorted(chosen)


def test_the_sample_is_the_whole_set_when_small():
    rows = [(1, "MC", False), (2, "MC", False)]
    assert choose_sample(rows, 15, seed=None) == [1, 2]


def test_a_seed_makes_the_draw_repeatable():
    rows = [(i, "MC", False) for i in range(100)]
    assert choose_sample(rows, 10, seed=3) == choose_sample(rows, 10, seed=3)


# --- Deterministic checks -------------------------------------------------


def _terms(document: str, version: int, markdown: str, *, debrief: bool = False) -> RaterTerms:
    return RaterTerms(
        consent=StatementText(document=document, version=version, body=markdown, source_url="x"),
        consent_markdown=markdown,
        consent_html="",
        debrief_html="<p>debrief</p>" if debrief else None,
    )


@pytest.mark.asyncio
async def test_consent_placeholder_is_an_error(config):
    terms = _terms("", 0, PLACEHOLDER_CONSENT)
    findings = await consent.run(_ctx(config, _experiment(), terms=terms), "error")
    assert _severities(findings) == ["error"]
    assert "TERMS__SOURCE_URL" in findings[0].message


@pytest.mark.asyncio
async def test_consent_unreadable_is_an_error(config):
    findings = await consent.run(_ctx(config, _experiment(), terms_error="source gone"), "error")
    assert _severities(findings) == ["error"]
    assert findings[0].detail == "source gone"


@pytest.mark.asyncio
async def test_consent_that_names_the_study_and_length_passes(config):
    exp = _experiment()
    terms = _terms("consent/standard", 2, f"Study {exp.name}. You have 1 hour.")
    findings = await consent.run(_ctx(config, exp, terms=terms), "error")
    assert _severities(findings) == ["info"]
    assert "standard v2" in findings[0].message


@pytest.mark.asyncio
async def test_consent_missing_placeholders_is_flagged(config):
    exp = _experiment()
    terms = _terms("consent/standard", 1, "Generic text with {{content_warning_details}} left in.")
    findings = await consent.run(_ctx(config, exp, terms=terms), "error")
    messages = " ".join(f.message for f in findings)
    assert "unfilled placeholders" in messages
    assert "study name" in messages and "session length" in messages


@pytest.mark.asyncio
async def test_markdown_structure_with_rendering_off_is_flagged(config):
    exp = _experiment(is_markdown=False, human_prompt_prefix="## Instructions\n- read")
    findings = await markdown_mode.run(_ctx(config, exp), "warning")
    assert _severities(findings) == ["warning"]
    assert "prompt prefix" in findings[0].message


@pytest.mark.asyncio
async def test_plain_text_with_rendering_off_passes(config):
    exp = _experiment(is_markdown=False, human_prompt_prefix="Read the passage, then answer.")
    assert await markdown_mode.run(_ctx(config, exp), "warning") == []


@pytest.mark.asyncio
async def test_unfilled_placeholder_in_a_question_is_flagged(config):
    exp = _experiment()
    view = _view(exp, text="Rate {{passage}} now")
    findings = await markdown_mode.run(_ctx(config, exp, [view]), "warning")
    assert [f.question_ids for f in findings] == [[1]]


@pytest.mark.asyncio
async def test_a_choice_question_without_options_is_an_error(config):
    exp = _experiment()
    views = [
        _view(exp, qid=1, options=None),
        _view(exp, qid=2, options="Yes|Yes"),
        _view(exp, qid=3, text="   "),
        _view(exp, qid=4, parent_id=9, parent_text=""),
        _view(exp, qid=5),
    ]
    findings = await question_renders.run(_ctx(config, exp, views), "error")
    by_question = {f.question_ids[0]: f for f in findings}
    assert set(by_question) == {1, 2, 3, 4}
    assert by_question[1].severity == "error"
    assert by_question[2].severity == "warning"
    assert "duplicates" in by_question[2].message
    assert by_question[3].severity == "error"
    assert "parent" in by_question[4].message


@pytest.mark.asyncio
async def test_assistance_checks_skip_on_a_control_arm(config):
    ctx = _ctx(config, _experiment(), probes=None)
    for check in (assistance_output, assistance_health):
        findings = await check.run(ctx, "error")
        assert _severities(findings) == ["info"]
        assert findings[0].message.startswith("Skipped")


@pytest.mark.asyncio
async def test_zero_confidence_empty_subtasks_are_an_error(config):
    exp = _experiment(assistance_method="human_as_a_tool")
    payload = {
        "subtasks": [
            {"index": 0, "question": "Is it true?", "my_answer": "", "confidence": 0},
            {"index": 1, "question": "Is it cited?", "my_answer": "", "confidence": 0},
        ],
        "confidence_threshold": 75,
    }
    ctx = _ctx(config, exp, probes=[_probe(1, "ask_input", payload)])
    findings = await assistance_output.run(ctx, "error")
    assert len(findings) == 1
    assert "every confidence is 0%" in findings[0].message
    assert (findings[0].severity, findings[0].question_ids) == ("error", [1])


@pytest.mark.asyncio
async def test_a_null_synthesis_answer_is_empty(config):
    exp = _experiment(assistance_method="human_as_a_tool")
    payload = {"synthesis": {"answer": None, "confidence": 80}}
    ctx = _ctx(config, exp, probes=[_probe(1, "complete", payload)])
    findings = await assistance_output.run(ctx, "error")
    assert len(findings) == 1
    assert "the answer is empty" in findings[0].message


@pytest.mark.asyncio
async def test_probes_run_as_speculative_calls(monkeypatch):
    exp = _experiment(assistance_method="top_n", assistance_params=None)
    seen: list[bool] = []

    async def fake_start(method, question, params, /, **kw):
        seen.append(speculative_call.get())
        step = InteractionStep(type=StepType.DISPLAY, payload={"kind": "top_n"})
        return _MethodCall(step=step, latency_ms=5, error=None)

    monkeypatch.setattr(probe_runner, "start_method", fake_start)
    probes = await probe_runner.probe_all(exp, [_view(exp, qid=1), _view(exp, qid=2)])
    assert [p.question_id for p in probes] == [1, 2]
    assert seen == [True, True]
    assert speculative_call.get() is False


@pytest.mark.asyncio
async def test_a_healthy_subtask_list_passes(config):
    exp = _experiment(assistance_method="human_as_a_tool")
    payload = {
        "subtasks": [
            {"index": 0, "question": "Is it true?", "my_answer": "yes", "confidence": 90},
            {"index": 1, "question": "Is it cited?", "my_answer": "no", "confidence": 40},
        ],
        "confidence_threshold": 75,
    }
    ctx = _ctx(config, exp, probes=[_probe(1, "ask_input", payload)])
    assert await assistance_output.run(ctx, "error") == []


@pytest.mark.asyncio
async def test_top_n_without_candidates_is_an_error(config):
    exp = _experiment(assistance_method="top_n")
    ctx = _ctx(
        config,
        exp,
        probes=[
            _probe(1, "display", {"kind": "top_n", "candidates": []}),
            _probe(
                2,
                "none",
                {"kind": "top_n", "parse_status": "no_candidates"},
                failure_reason="invalid_response",
            ),
        ],
    )
    findings = await assistance_output.run(ctx, "error")
    assert [(f.question_ids, f.severity) for f in findings] == [([1], "error"), ([2], "error")]
    assert "invalid_response" in findings[1].message


@pytest.mark.asyncio
async def test_health_reports_failures_and_identical_output(config):
    exp = _experiment(assistance_method="top_n")
    same = {"kind": "top_n", "candidates": [{"rank": 1, "answer": "Yes"}]}
    ctx = _ctx(
        config,
        exp,
        probes=[
            _probe(1, "display", same),
            _probe(2, "display", same),
            _probe(3, "none", failure_reason="provider_error", failure_detail="401"),
        ],
    )
    findings = await assistance_health.run(ctx, "error")
    messages = [f.message for f in findings]
    assert any("1 of 3" in m and "provider_error" in m for m in messages)
    assert any("byte-identical" in m for m in messages)
    assert findings[0].detail == "401"


@pytest.mark.asyncio
async def test_health_passes_with_an_info_line(config):
    exp = _experiment(assistance_method="top_n")
    ctx = _ctx(
        config,
        exp,
        probes=[
            _probe(1, "display", {"candidates": [{"answer": "Yes"}]}),
            _probe(2, "display", {"candidates": [{"answer": "No"}]}),
        ],
    )
    findings = await assistance_health.run(ctx, "error")
    assert _severities(findings) == ["info"]
    assert "2 of 2" in findings[0].message


@pytest.mark.asyncio
async def test_slow_assistance_is_a_warning(config):
    exp = _experiment(assistance_method="top_n")
    ctx = _ctx(
        config,
        exp,
        probes=[_probe(1, "display", {"candidates": [{"answer": "Yes"}]}, latency_ms=45_000)],
    )
    findings = await assistance_health.run(ctx, "error")
    assert _severities(findings) == ["warning"]
    assert "45.0s" in findings[0].message


# --- Judged checks --------------------------------------------------------


def test_verdict_parsing_tolerates_fences_and_rejects_junk():
    verdict = parse_verdict('```json\n{"ok": false, "issues": ["bad"], "summary": "no"}\n```')
    assert verdict == Verdict(ok=False, issues=["bad"], summary="no")
    with pytest.raises(JudgeError):
        parse_verdict("not json")
    with pytest.raises(JudgeError):
        parse_verdict('{"issues": []}')


class _FakeJudge:
    model = "fake/judge"

    def __init__(
        self,
        verdict: Verdict | Exception,
        problems: list[Problem] | Exception | None = None,
    ):
        self.verdict = verdict
        self.problems = problems if problems is not None else JudgeError("no grouping")
        self.calls: list[str] = []
        self.group_calls: list[str] = []

    async def __call__(self, *, task: str, material: str) -> Verdict:
        self.calls.append(material)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict

    async def group(self, *, task: str, material: str) -> list[Problem]:
        self.group_calls.append(material)
        if isinstance(self.problems, Exception):
            raise self.problems
        return self.problems


def test_problem_parsing_keeps_only_numeric_ids():
    problems = parse_problems(
        '{"problems": [{"summary": "Options missing", "question_ids": [1, "2", "x"], '
        '"issues": ["no option 4", ""]}, "junk"]}'
    )
    assert problems == [Problem("Options missing", [1, 2], ["no option 4"])]
    with pytest.raises(JudgeError):
        parse_problems('{"ok": true}')


@pytest.mark.asyncio
async def test_flagged_questions_are_merged_into_one_finding_per_problem(config):
    exp = _experiment()
    views = [_view(exp, qid=1), _view(exp, qid=2), _view(exp, qid=3)]
    judge = _FakeJudge(
        Verdict(ok=False, issues=["type says FT but the task is true/false"], summary="Mismatch"),
        problems=[Problem("Declared free text, really true/false", [1, 2], ["per option"])],
    )
    findings = await question_coherent.run(_ctx(config, exp, views, judge=judge), "warning")
    assert [(f.message, f.question_ids) for f in findings] == [
        ("Declared free text, really true/false", [1, 2]),
        ("Mismatch", [3]),  # left out of the merge, so it keeps its own row
    ]
    assert findings[0].detail == "- per option"
    assert len(judge.group_calls) == 1
    assert "[Question 3]" in judge.group_calls[0]


@pytest.mark.asyncio
async def test_a_failed_merge_keeps_the_per_question_findings(config):
    exp = _experiment()
    views = [_view(exp, qid=1), _view(exp, qid=2)]
    judge = _FakeJudge(Verdict(ok=False, issues=["x"], summary="Bad"))
    findings = await question_coherent.run(_ctx(config, exp, views, judge=judge), "warning")
    assert [(f.message, f.question_ids) for f in findings] == [("Bad", [1]), ("Bad", [2])]


@pytest.mark.asyncio
async def test_one_flagged_question_is_not_sent_for_merging(config):
    exp = _experiment()
    judge = _FakeJudge(Verdict(ok=False, issues=["x"], summary="Bad"), problems=[])
    findings = await question_coherent.run(_ctx(config, exp, [_view(exp)], judge=judge), "warning")
    assert [f.question_ids for f in findings] == [[1]]
    assert judge.group_calls == []


@pytest.mark.asyncio
async def test_a_judged_check_without_a_judge_is_skipped(config):
    ctx = _ctx(config, _experiment(), judge=None, judge_unavailable_reason="no key")
    findings = await instructions.run(ctx, "warning")
    assert _severities(findings) == ["info"]
    assert "no key" in findings[0].message


@pytest.mark.asyncio
async def test_a_judged_check_turns_the_verdict_into_a_finding(config):
    judge = _FakeJudge(Verdict(ok=False, issues=["names a 1-5 scale"], summary="Scale mismatch"))
    exp = _experiment(human_prompt_suffix="Pick one.")
    findings = await instructions.run(_ctx(config, exp, judge=judge), "warning")
    assert len(findings) == 1
    assert findings[0].message == "Scale mismatch"
    assert findings[0].detail == "- names a 1-5 scale"
    # The judge saw the instructions, the framing and an example question.
    assert "Rate each passage" in judge.calls[0]
    assert "Pick one." in judge.calls[0]
    assert "[Example question 1]" in judge.calls[0]


class _FlakyJudge(_FakeJudge):
    """Answers from a script of verdicts and exceptions, one per call."""

    def __init__(self, script: list[Verdict | Exception]):
        super().__init__(Verdict(ok=True, summary="", issues=[]))
        self.script = list(script)

    async def __call__(self, *, task: str, material: str) -> Verdict:
        self.calls.append(material)
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_one_failed_judge_call_does_not_stop_the_others(config):
    exp = _experiment()
    views = [_view(exp, qid=qid) for qid in (1, 2, 3)]
    bad = Verdict(ok=False, summary="Options missing", issues=["no options"])
    judge = _FlakyJudge([JudgeError("timeout"), Verdict(ok=True, summary="", issues=[]), bad])
    findings = await question_coherent.run(_ctx(config, exp, views=views, judge=judge), "warning")
    assert len(judge.calls) == 3
    assert [(f.message, f.question_ids) for f in findings] == [
        ("The judge call failed on this question.", [1]),
        ("Options missing", [3]),
    ]


@pytest.mark.asyncio
async def test_three_judge_failures_in_a_row_stop_the_check(config):
    exp = _experiment()
    views = [_view(exp, qid=qid) for qid in (1, 2, 3, 4, 5)]
    judge = _FakeJudge(JudgeError("boom"))
    findings = await question_coherent.run(_ctx(config, exp, views=views, judge=judge), "warning")
    assert len(judge.calls) == 3
    assert [f.question_ids for f in findings] == [[1], [2], [3], []]
    assert findings[-1].message == (
        "The judge failed 3 times in a row, so the remaining 2 questions were not reviewed."
    )
    assert {f.severity for f in findings} == {"warning"}


@pytest.mark.asyncio
async def test_a_failing_judge_is_a_warning(config):
    judge = _FakeJudge(JudgeError("boom"))
    findings = await instructions.run(_ctx(config, _experiment(), judge=judge), "warning")
    assert _severities(findings) == ["warning"]
    assert findings[0].detail == "boom"


# --- Summary and fingerprint ---------------------------------------------


def _run(**overrides) -> PilotCheckRun:
    values = {
        "id": 3,
        "experiment_id": 7,
        "status": "passed",
        "fingerprint": "fp",
        "config_sha256": "cfg",
        "findings": "[]",
        "created_at": datetime.now(UTC) - timedelta(minutes=1),
        "finished_at": datetime.now(UTC),
    }
    values.update(overrides)
    return PilotCheckRun(**values)


def test_no_run_needs_acknowledgement_unless_the_gate_is_off(config):
    summary = summarise(None, current_fingerprint="fp", settings=config)
    assert summary.state == "not_run"
    assert summary.needs_acknowledgement is True
    off = PilotCheckSettings(gate_mode="off")
    assert summarise(None, current_fingerprint="fp", settings=off).needs_acknowledgement is False


def test_a_clean_run_needs_nothing(config):
    summary = summarise(_run(), current_fingerprint="fp", settings=config)
    assert summary.state == "passed"
    assert summary.needs_acknowledgement is False
    assert summary.reason is None


def test_an_upload_starts_a_run_only_when_there_is_something_new_to_check():
    now = datetime.now(UTC)

    def reason(run, fingerprint="fp"):
        return autostart_skip_reason(run, current_fingerprint=fingerprint, now=now)

    assert reason(None) is None
    assert reason(_run()) == "the latest run is still fresh"
    assert reason(_run(status="failed")) == "the latest run is still fresh"
    assert reason(_run(), fingerprint="other") is None
    live = _run(status="running", finished_at=None, updated_at=now)
    assert reason(live, fingerprint="other") == "a run is already in progress"
    # Stopped reporting progress, so aborted: nothing covers the experiment.
    gone = _run(status="running", finished_at=None, updated_at=now - timedelta(minutes=10))
    assert reason(gone) is None
    assert reason(_run(status="aborted")) is None


def test_a_queued_run_is_in_progress_until_no_worker_could_have_claimed_it(config):
    now = datetime.now(UTC)
    queued = _run(status="queued", finished_at=None, created_at=now - timedelta(minutes=2))
    assert effective_status(queued, now) == "queued"
    summary = summarise(queued, current_fingerprint="fp", settings=config, now=now)
    assert summary.state == "running"
    assert summary.reason == "The pilot check is queued and starts shortly."
    assert summary.needs_acknowledgement is True
    skip = autostart_skip_reason(queued, current_fingerprint="other", now=now)
    assert skip == "a run is already in progress"

    unclaimed = _run(
        status="queued", finished_at=None, created_at=now - QUEUE_TIMEOUT - timedelta(seconds=1)
    )
    assert effective_status(unclaimed, now) == "aborted"
    assert autostart_skip_reason(unclaimed, current_fingerprint="fp", now=now) is None


def test_a_changed_experiment_makes_the_run_stale(config):
    summary = summarise(_run(), current_fingerprint="other", settings=config)
    assert summary.state == "stale"
    assert summary.needs_acknowledgement is True


def test_warnings_need_acknowledgement(config):
    findings = json.dumps([{"check": "x", "severity": "warning", "message": "m"}])
    run = _run(status="warnings", findings=findings)
    summary = summarise(run, current_fingerprint="fp", settings=config)
    assert summary.state == "warnings"
    assert summary.counts["warning"] == 1
    assert summary.needs_acknowledgement is True
    assert "1 warning" in summary.reason


def test_a_run_that_stopped_reporting_progress_reads_as_aborted(config):
    stale = datetime.now(UTC) - timedelta(minutes=10)
    run = _run(status="running", created_at=stale, updated_at=stale)
    assert summarise(run, current_fingerprint="fp", settings=config).state == "aborted"
    fresh = _run(status="running", created_at=stale, updated_at=datetime.now(UTC))
    assert summarise(fresh, current_fingerprint="fp", settings=config).state == "running"


def test_the_fingerprint_moves_with_what_raters_see():
    exp = _experiment()
    base = compute_fingerprint(exp, question_count=10, latest_upload_id=1, config_sha256="c")
    assert base == compute_fingerprint(
        exp, question_count=10, latest_upload_id=1, config_sha256="c"
    )
    exp.description = "Changed."
    assert base != compute_fingerprint(
        exp, question_count=10, latest_upload_id=1, config_sha256="c"
    )
    exp.description = "Rate each passage for accuracy."
    assert base != compute_fingerprint(
        exp, question_count=10, latest_upload_id=2, config_sha256="c"
    )
    assert base != compute_fingerprint(
        exp, question_count=10, latest_upload_id=1, config_sha256="d"
    )


@pytest.mark.asyncio
async def test_consent_pinned_to_the_wrong_statement_for_the_flags_is_an_error(config):
    exp = _experiment(study_flags=["sensitive"], content_warning_details="Violence.")
    terms = _terms("consent/standard", 1, f"Study {exp.name}. You have 1 hour. Violence.")
    findings = await consent.run(_ctx(config, exp, terms=terms), "error")
    assert _severities(findings) == ["error"]
    assert "call for the warned one" in findings[0].message


@pytest.mark.asyncio
async def test_consent_for_a_deception_study_without_a_debrief_is_an_error(config):
    exp = _experiment(study_flags=["deception"])
    terms = _terms("consent/standard", 1, f"Study {exp.name}. You have 1 hour.")
    findings = await consent.run(_ctx(config, exp, terms=terms), "error")
    assert _severities(findings) == ["error"]
    assert "debrief" in findings[0].message
    with_debrief = _terms(
        "consent/standard", 1, f"Study {exp.name}. You have 1 hour.", debrief=True
    )
    findings = await consent.run(_ctx(config, exp, terms=with_debrief), "error")
    assert _severities(findings) == ["info"]
    assert "debrief at the end" in findings[0].message


@pytest.mark.asyncio
async def test_consent_for_a_warned_study_without_details_is_an_error(config):
    exp = _experiment(study_flags=["sensitive"], content_warning_details="")
    terms = _terms("consent/warned", 1, f"Study {exp.name}. You have 1 hour.")
    findings = await consent.run(_ctx(config, exp, terms=terms), "error")
    assert findings[0].severity == "error"
    assert "Content warning details are required" in findings[0].message


@pytest.mark.asyncio
async def test_markdown_in_the_prefix_passes_with_rendering_on(config):
    # The rater card renders the prefix and suffix under the same switch.
    exp = _experiment(is_markdown=True, human_prompt_prefix="## Instructions\n- read")
    assert await markdown_mode.run(_ctx(config, exp), "warning") == []


@pytest.mark.asyncio
async def test_raw_html_in_the_suffix_is_flagged_only_with_rendering_on(config):
    suffix = "Pick <b>one</b>."
    on = _experiment(is_markdown=True, human_prompt_suffix=suffix)
    findings = await markdown_mode.run(_ctx(config, on), "warning")
    assert _severities(findings) == ["warning"]
    assert "prompt suffix contains raw HTML" in findings[0].message
    off = _experiment(is_markdown=False, human_prompt_suffix=suffix)
    assert await markdown_mode.run(_ctx(config, off), "warning") == []


def test_the_judge_is_told_the_framing_follows_the_markdown_switch():
    on = _experiment(is_markdown=True, human_prompt_prefix="**Read** first")
    assert "[Shown above the question, as Markdown]" in _view(on).as_text()
    off = _experiment(is_markdown=False, human_prompt_prefix="**Read** first")
    assert "[Shown above the question, as plain text]" in _view(off).as_text()


@pytest.mark.asyncio
async def test_missing_warning_details_do_not_hide_the_wrong_statement(config):
    exp = _experiment(study_flags=["sensitive"], content_warning_details="")
    terms = _terms("consent/standard", 1, f"Study {exp.name}. You have 1 hour.")
    findings = await consent.run(_ctx(config, exp, terms=terms), "error")
    messages = [f.message for f in findings]
    assert any("Content warning details are required" in m for m in messages)
    assert any("call for the warned one" in m for m in messages)


@pytest.mark.asyncio
async def test_a_check_that_crashes_is_a_warning(config):
    async def boom(ctx, severity):
        raise RuntimeError("nope")

    spec = CheckSpec("boom", "Boom", boom, "error")
    findings = await _run_check(1, _ctx(config, _experiment()), spec)
    assert _severities(findings) == ["warning"]
    assert findings[0].detail and "nope" in findings[0].detail


def test_severity_overrides_must_name_a_check():
    validate_severities(PilotCheckSettings(severities={"markdown_mode": "info"}))
    with pytest.raises(ValueError, match="markdwon_mode"):
        validate_severities(PilotCheckSettings(severities={"markdwon_mode": "info"}))


def test_the_fingerprint_ignores_the_pinned_statement_ids():
    exp = _experiment()
    base = compute_fingerprint(exp, question_count=10, latest_upload_id=1, config_sha256="c")
    exp.consent_statement_id = 7
    exp.debrief_statement_id = 8
    assert base == compute_fingerprint(
        exp, question_count=10, latest_upload_id=1, config_sha256="c"
    )


def test_findings_that_say_the_same_thing_merge_into_one_row():
    same = "The options contain duplicates."
    merged = merge_findings(
        [
            Finding("question_renders", "warning", same, question_ids=[1], detail="a|a"),
            Finding("question_renders", "warning", same, question_ids=[2], detail="a|a"),
            Finding("question_renders", "warning", same, question_ids=[3], detail="b|b"),
            Finding("question_renders", "error", "The question text is empty.", question_ids=[2]),
            Finding("consent_configured", "info", "Raters see v1."),
        ]
    )
    assert [(f.message, f.question_ids, f.detail) for f in merged] == [
        (same, [1, 2], "a|a"),
        (same, [3], "b|b"),
        ("The question text is empty.", [2], None),
        ("Raters see v1.", [], None),
    ]


# --- Rater screens ----------------------------------------------------------


class _LookingJudge(_FakeJudge):
    """Reads screenshots: the verdict for each PNG's bytes, ok when unlisted."""

    def __init__(self, by_png: dict[bytes, Verdict | Exception]):
        super().__init__(Verdict(ok=True, summary="", issues=[]))
        self.by_png = by_png
        self.looked: list[bytes] = []

    async def look(self, *, task: str, png: bytes) -> Verdict:
        self.looked.append(png)
        outcome = self.by_png.get(png, Verdict(ok=True, summary="", issues=[]))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _shots(*question_ids: int, intro: bool = True) -> list[browser.Screen]:
    shots = [browser.Screen(browser.CONSENT, b"consent")]
    if intro:
        shots.append(browser.Screen(browser.INTRO, b"intro"))
    shots += [
        browser.Screen(browser.question_screen(q), f"q{q}".encode(), question_id=q)
        for q in question_ids
    ]
    return shots


def _browser_returns(monkeypatch, result):
    calls = []

    async def fake_capture(**kwargs):
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(browser, "capture", fake_capture)
    return calls


@pytest.mark.asyncio
async def test_rater_screens_skip_without_an_app_url(config, monkeypatch):
    calls = _browser_returns(monkeypatch, _shots(1))
    ctx = _ctx(config, _experiment(), judge=_LookingJudge({}))
    findings = await rater_screens.run(ctx, "warning")
    assert [f.severity for f in findings] == ["info"]
    assert "rater_app_url" in findings[0].message
    assert calls == []


@pytest.mark.asyncio
async def test_rater_screens_flag_what_the_judge_sees_and_link_the_screenshot(monkeypatch):
    settings = PilotCheckSettings(rater_app_url="https://app.example")
    exp = _experiment()
    views = [_view(exp, qid=1), _view(exp, qid=2)]
    calls = _browser_returns(monkeypatch, _shots(1, 2))
    judge = _LookingJudge(
        {
            b"consent": Verdict(ok=False, summary="The consent text is cut off.", issues=["x"]),
            b"q2": Verdict(ok=False, summary="Raw LaTeX shows.", issues=["$x^2$"]),
        }
    )
    ctx = _ctx(settings, exp, views, judge=judge, run_id=42)
    findings = await rater_screens.run(ctx, "warning")

    assert calls[0]["app_url"] == "https://app.example"
    assert calls[0]["question_ids"] == [1, 2] and calls[0]["run_id"] == 42
    assert judge.looked == [b"consent", b"intro", b"q1", b"q2"]
    by_message = {f.message: f for f in findings}
    assert by_message["The consent text is cut off."].screens == ["consent"]
    assert by_message["The consent text is cut off."].question_ids == []
    latex = by_message["Raw LaTeX shows."]
    assert latex.question_ids == [2] and latex.screens == ["question-2"]
    assert latex.detail == "- $x^2$"
    # Every screen is handed to the run; it keeps only the ones findings name.
    assert set(ctx.screenshots) == {"consent", "intro", "question-1", "question-2"}


@pytest.mark.asyncio
async def test_a_screen_that_never_loaded_is_a_finding_with_its_screenshot(monkeypatch):
    settings = PilotCheckSettings(rater_app_url="https://app.example")
    exp = _experiment()
    shots = _shots(1, intro=False)
    shots[1].problems.append("The assistance was still loading after two minutes.")
    _browser_returns(monkeypatch, shots)
    ctx = _ctx(settings, exp, [_view(exp, qid=1)], judge=_LookingJudge({}))
    findings = await rater_screens.run(ctx, "warning")
    assert [(f.message, f.question_ids, f.screens) for f in findings] == [
        ("The assistance was still loading after two minutes.", [1], ["question-1"])
    ]


@pytest.mark.asyncio
async def test_no_browser_on_the_server_is_a_warning(monkeypatch):
    settings = PilotCheckSettings(rater_app_url="https://app.example")
    _browser_returns(monkeypatch, browser.BrowserUnavailable("Executable doesn't exist"))
    ctx = _ctx(settings, _experiment(), judge=_LookingJudge({}))
    findings = await rater_screens.run(ctx, "warning")
    assert [f.severity for f in findings] == ["warning"]
    assert "could not open a browser" in findings[0].message
    assert findings[0].detail == "Executable doesn't exist"


@pytest.mark.asyncio
async def test_three_failed_looks_in_a_row_stop_the_screen_review(monkeypatch):
    settings = PilotCheckSettings(rater_app_url="https://app.example")
    exp = _experiment()
    _browser_returns(monkeypatch, _shots(1, 2, 3))
    failing = {png: JudgeError("down") for png in (b"consent", b"intro", b"q1", b"q2", b"q3")}
    judge = _LookingJudge(failing)
    ctx = _ctx(settings, exp, [_view(exp, qid=q) for q in (1, 2, 3)], judge=judge)
    findings = await rater_screens.run(ctx, "warning")
    assert len(judge.looked) == 3
    assert "remaining 2 screens were not reviewed" in findings[-1].message


def test_the_screen_stage_needs_a_rater_app_url(monkeypatch):
    monkeypatch.setattr("services.pilot_check.runs.judge_unavailable_reason", lambda model: None)
    exp = _experiment()
    without = [stage.id for stage in plan_stages(exp, PilotCheckSettings())]
    with_url = [
        stage.id
        for stage in plan_stages(exp, PilotCheckSettings(rater_app_url="https://app.example"))
    ]
    assert "rater_screens" not in without
    assert with_url[-2:] == ["rater_screens", "done"]


def test_merging_findings_keeps_every_screenshot():
    a = Finding("rater_screens", "warning", "Same", question_ids=[1], screens=["question-1"])
    b = Finding("rater_screens", "warning", "Same", question_ids=[2], screens=["question-2"])
    (merged,) = merge_findings([a, b])
    assert merged.question_ids == [1, 2]
    assert merged.screens == ["question-1", "question-2"]
