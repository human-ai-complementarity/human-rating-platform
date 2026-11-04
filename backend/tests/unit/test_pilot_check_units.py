"""Unit tests for the pilot check (docs/pilot-check.md): config loading, the
sample, the deterministic checks against hand-built contexts, the judge's
reply parsing, and the summary the gate and the UI read."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import BASE_DIR
from models import Experiment, ExperimentStatus, PilotCheckRun, Question
from services.pilot_check.assistance import AssistanceProbe
from services.pilot_check.checks.assistance_health import AssistanceHealth
from services.pilot_check.checks.assistance_output import AssistanceOutput
from services.pilot_check.checks.base import CheckContext
from services.pilot_check.checks.consent import ConsentConfigured
from services.pilot_check.checks.instructions import InstructionsCoherent
from services.pilot_check.checks.markdown_mode import MarkdownMode
from services.pilot_check.checks.question_renders import QuestionRenders, parse_options
from services.pilot_check.checks.registry import check_names
from services.pilot_check.config import PilotCheckConfigError, load_config, parse_config
from services.pilot_check.judge import JudgeError, Verdict, parse_verdict
from services.pilot_check.rater_view import RaterQuestionView
from services.pilot_check.runs import compute_fingerprint, summarise
from services.pilot_check.sampling import choose_sample
from services.rater.mappers import build_question_response
from services.terms.service import PLACEHOLDER_CONSENT, RaterTerms, StatementText

CONFIG_FILE = BASE_DIR / "pilot_check.toml"


@pytest.fixture(scope="module")
def config():
    return load_config(CONFIG_FILE, known_checks=check_names())


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
    return RaterQuestionView(
        response=build_question_response(question, experiment.is_markdown, parent_text),
        human_prompt_prefix=experiment.human_prompt_prefix,
        human_prompt_suffix=experiment.human_prompt_suffix,
        question=question,
    )


def _ctx(config, experiment: Experiment, views=None, **kwargs) -> CheckContext:
    return CheckContext(
        experiment=experiment,
        config=config,
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


# --- Config ----------------------------------------------------------------


def test_the_shipped_config_lists_every_check(config):
    assert set(config.checks) == set(check_names())
    assert config.sha256
    assert config.gate.mode == "warn"


def test_an_unknown_check_name_is_refused(config):
    text = CONFIG_FILE.read_text() + "\n[checks.made_up]\nenabled = true\nseverity = 'info'\n"
    with pytest.raises(PilotCheckConfigError, match="do not exist: made_up"):
        parse_config(text, known_checks=check_names())


def test_a_missing_check_is_refused():
    text = "[checks.consent_configured]\nenabled = true\nseverity = 'error'\n"
    with pytest.raises(PilotCheckConfigError, match="missing: assistance_health"):
        parse_config(text, known_checks=check_names())


def test_an_unknown_key_is_refused():
    text = CONFIG_FILE.read_text().replace("[sample]\n", "[sample]\nsise = 3\n")
    with pytest.raises(PilotCheckConfigError, match="invalid"):
        parse_config(text, known_checks=check_names())


def test_a_missing_file_is_refused(tmp_path: Path):
    with pytest.raises(PilotCheckConfigError, match="not readable"):
        load_config(tmp_path / "nope.toml", known_checks=check_names())


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
    findings = await ConsentConfigured().run(_ctx(config, _experiment(), terms=terms), "error")
    assert _severities(findings) == ["error"]
    assert "TERMS__SOURCE_URL" in findings[0].message


@pytest.mark.asyncio
async def test_consent_unreadable_is_an_error(config):
    findings = await ConsentConfigured().run(
        _ctx(config, _experiment(), terms_error="source gone"), "error"
    )
    assert _severities(findings) == ["error"]
    assert findings[0].detail == "source gone"


@pytest.mark.asyncio
async def test_consent_that_names_the_study_and_length_passes(config):
    exp = _experiment()
    terms = _terms("consent/standard", 2, f"Study {exp.name}. You have 1 hour.")
    findings = await ConsentConfigured().run(_ctx(config, exp, terms=terms), "error")
    assert _severities(findings) == ["info"]
    assert "standard v2" in findings[0].message


@pytest.mark.asyncio
async def test_consent_missing_placeholders_is_flagged(config):
    exp = _experiment()
    terms = _terms("consent/standard", 1, "Generic text with {{content_warning_details}} left in.")
    findings = await ConsentConfigured().run(_ctx(config, exp, terms=terms), "error")
    messages = " ".join(f.message for f in findings)
    assert "unfilled placeholders" in messages
    assert "study name" in messages and "session length" in messages


@pytest.mark.asyncio
async def test_markdown_structure_with_rendering_off_is_flagged(config):
    exp = _experiment(is_markdown=False, human_prompt_prefix="## Instructions\n- read")
    findings = await MarkdownMode().run(_ctx(config, exp), "warning")
    assert _severities(findings) == ["warning"]
    assert "prompt prefix" in findings[0].message


@pytest.mark.asyncio
async def test_plain_text_with_rendering_off_passes(config):
    exp = _experiment(is_markdown=False, human_prompt_prefix="Read the passage, then answer.")
    assert await MarkdownMode().run(_ctx(config, exp), "warning") == []


@pytest.mark.asyncio
async def test_unfilled_placeholder_in_a_question_is_flagged(config):
    exp = _experiment()
    view = _view(exp, text="Rate {{passage}} now")
    findings = await MarkdownMode().run(_ctx(config, exp, [view]), "warning")
    assert [f.question_id for f in findings] == [1]


def test_options_parse_like_the_rater_card():
    assert parse_options("Yes|No") == ["Yes", "No"]
    assert parse_options("A. one B. two") == ["A. one", "B. two"]
    assert parse_options("one\ntwo\nthree") == ["one", "two", "three"]
    assert parse_options("one, two") == ["one", "two"]
    assert parse_options(None) == []


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
    findings = await QuestionRenders().run(_ctx(config, exp, views), "error")
    by_question = {f.question_id: f for f in findings}
    assert set(by_question) == {1, 2, 3, 4}
    assert by_question[1].severity == "error"
    assert by_question[2].severity == "warning"
    assert "duplicates" in by_question[2].message
    assert by_question[3].severity == "error"
    assert "parent" in by_question[4].message


@pytest.mark.asyncio
async def test_assistance_checks_skip_on_a_control_arm(config):
    ctx = _ctx(config, _experiment(), probes=None)
    for check in (AssistanceOutput(), AssistanceHealth()):
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
    findings = await AssistanceOutput().run(ctx, "error")
    messages = [f.message for f in findings]
    assert any("0% confidence" in m for m in messages)
    assert any("No subtask has an AI answer" in m for m in messages)
    assert all(f.severity == "error" and f.question_id == 1 for f in findings)


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
    assert await AssistanceOutput().run(ctx, "error") == []


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
    findings = await AssistanceOutput().run(ctx, "error")
    assert [(f.question_id, f.severity) for f in findings] == [(1, "error"), (2, "error")]
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
    findings = await AssistanceHealth().run(ctx, "error")
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
    findings = await AssistanceHealth().run(ctx, "error")
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
    findings = await AssistanceHealth().run(ctx, "error")
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

    def __init__(self, verdict: Verdict | Exception):
        self.verdict = verdict
        self.calls: list[str] = []

    async def __call__(self, *, task: str, material: str) -> Verdict:
        self.calls.append(material)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


@pytest.mark.asyncio
async def test_a_judged_check_without_a_judge_is_skipped(config):
    ctx = _ctx(config, _experiment(), judge=None, judge_unavailable_reason="no key")
    findings = await InstructionsCoherent().run(ctx, "warning")
    assert _severities(findings) == ["info"]
    assert "no key" in findings[0].message


@pytest.mark.asyncio
async def test_a_judged_check_turns_the_verdict_into_a_finding(config):
    judge = _FakeJudge(Verdict(ok=False, issues=["names a 1-5 scale"], summary="Scale mismatch"))
    exp = _experiment(human_prompt_suffix="Pick one.")
    findings = await InstructionsCoherent().run(_ctx(config, exp, judge=judge), "warning")
    assert len(findings) == 1
    assert findings[0].message == "Scale mismatch"
    assert findings[0].detail == "- names a 1-5 scale"
    # The judge saw the instructions, the framing and an example question.
    assert "Rate each passage" in judge.calls[0]
    assert "Pick one." in judge.calls[0]
    assert "[Example question 1]" in judge.calls[0]


@pytest.mark.asyncio
async def test_a_failing_judge_is_an_info_finding(config):
    judge = _FakeJudge(JudgeError("boom"))
    findings = await InstructionsCoherent().run(_ctx(config, _experiment(), judge=judge), "warning")
    assert _severities(findings) == ["info"]
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
    summary = summarise(None, current_fingerprint="fp", config=config)
    assert summary.state == "not_run"
    assert summary.needs_acknowledgement is True
    off = config.model_copy(deep=True)
    off.gate.mode = "off"
    assert summarise(None, current_fingerprint="fp", config=off).needs_acknowledgement is False


def test_a_clean_run_needs_nothing(config):
    summary = summarise(_run(), current_fingerprint="fp", config=config)
    assert summary.state == "passed"
    assert summary.needs_acknowledgement is False
    assert summary.reason is None


def test_a_changed_experiment_makes_the_run_stale(config):
    summary = summarise(_run(), current_fingerprint="other", config=config)
    assert summary.state == "stale"
    assert summary.needs_acknowledgement is True


def test_warnings_need_acknowledgement_at_the_gate_severity(config):
    findings = json.dumps([{"check": "x", "severity": "warning", "message": "m"}])
    run = _run(status="warnings", findings=findings)
    summary = summarise(run, current_fingerprint="fp", config=config)
    assert summary.state == "warnings"
    assert summary.counts["warning"] == 1
    assert summary.needs_acknowledgement is True
    lenient = config.model_copy(deep=True)
    lenient.gate.severity = "error"
    assert summarise(run, current_fingerprint="fp", config=lenient).needs_acknowledgement is False


def test_a_run_past_its_deadline_reads_as_aborted(config):
    run = _run(status="running", created_at=datetime.now(UTC) - timedelta(days=1))
    assert summarise(run, current_fingerprint="fp", config=config).state == "aborted"
    fresh = _run(status="running", created_at=datetime.now(UTC))
    assert summarise(fresh, current_fingerprint="fp", config=config).state == "running"


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
    findings = await ConsentConfigured().run(_ctx(config, exp, terms=terms), "error")
    assert _severities(findings) == ["error"]
    assert "call for the warned one" in findings[0].message


@pytest.mark.asyncio
async def test_consent_for_a_deception_study_without_a_debrief_is_an_error(config):
    exp = _experiment(study_flags=["deception"])
    terms = _terms("consent/standard", 1, f"Study {exp.name}. You have 1 hour.")
    findings = await ConsentConfigured().run(_ctx(config, exp, terms=terms), "error")
    assert _severities(findings) == ["error"]
    assert "debrief" in findings[0].message
    with_debrief = _terms(
        "consent/standard", 1, f"Study {exp.name}. You have 1 hour.", debrief=True
    )
    findings = await ConsentConfigured().run(_ctx(config, exp, terms=with_debrief), "error")
    assert _severities(findings) == ["info"]
    assert "debrief at the end" in findings[0].message


@pytest.mark.asyncio
async def test_consent_for_a_warned_study_without_details_is_an_error(config):
    exp = _experiment(study_flags=["sensitive"], content_warning_details="")
    terms = _terms("consent/warned", 1, f"Study {exp.name}. You have 1 hour.")
    findings = await ConsentConfigured().run(_ctx(config, exp, terms=terms), "error")
    assert findings[0].severity == "error"
    assert "Content warning details are required" in findings[0].message
