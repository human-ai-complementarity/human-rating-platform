# Pilot check: an AI spot check before a study goes to raters

Design for an admin-triggered check that samples an experiment's questions,
walks each one through the same code path a rater hits, runs the configured
assistance method on it, and reports anything a researcher would want to fix
before paying Prolific participants to look at it. The experiment and launch
pages say whether the check has been run, show its findings, and ask before
creating a study without a clean run.

Status: implemented on branch `sander/pilot-check`. Code under
`backend/services/pilot_check/`, config in `backend/pilot_check.toml`, UI in
`frontend/src/components/experiment-detail/PilotCheckCard.tsx` and
`pilotCheckGate.tsx`. The sections below are the design as built; the
"Implementation notes" at the end list where the build departed from it.

## Why

The launch gate (#96, `services/admin/status.py`) answers "is the experiment
configured at all": questions uploaded, instructions present, a pinned model
with a key. It cannot answer "does any of this make sense to a rater". Today
that question is answered by an admin clicking **Preview as participant** and
rating a few questions by hand, which nobody does for every arm of every wave.
The 2026-07-31 outage showed what it costs when nobody looks: assisted arms
ran for days with the method returning no assistance on every question.

The check is the automated version of that preview: it looks at the same
strings a rater receives, runs the same method entry point, and flags the
things an attentive researcher would notice in ten minutes.

## What it checks

Each check is a small class with a stable name. The config file (below)
turns them on and off and sets their severity. There are two kinds:

- **Deterministic** checks read platform state and apply rules. They need no
  model and always run when enabled.
- **Judged** checks hand the rater-facing material to an LLM judge with a
  rubric and a JSON schema, and turn its answer into findings. They are
  skipped, with a visible "skipped: no judge key" finding, when the judge
  model's provider has no key on the server.

### Experiment-level checks (run once per check run)

| name | kind | what it flags |
| --- | --- | --- |
| `consent_configured` | deterministic | `TERMS__SOURCE_URL` unset (placeholder statement would be shown), source unreadable (the 503 raters would get), unknown placeholders left in the rendered text, rendered statement missing the study name or session length. The study flags' own rules: a sensitive or explicit study with no content warning details, the pinned statement not the one the flags select (standard vs warned), a deception study with no debrief, and the warned statement missing the promised details. |
| `launch_blockers` | deterministic | Whatever `experiment_launch_blockers` reports. Duplicated here so a run's report is complete on its own, not because the gate stops working. |
| `instructions_coherent` | judged | The rater instructions (`description`, as rendered on the intro screen) read as instructions for the task the questions pose: they name what to rate and how, they do not contradict the prompt prefix/suffix, and they do not mention an assistance method that is off (or omit one that is on). The judge sees the instructions, the prefix/suffix, the method's `rater_instructions`, and three sampled questions for context. |
| `markdown_mode` | deterministic | `is_markdown` is false but the description, prefix, suffix or sampled question text contains Markdown structure (headings, lists, fenced code, links), or is true and the text contains raw HTML or stray `{{placeholders}}`. |
| `assistance_health` | deterministic | Aggregate over the sampled assistance runs: the share of `none`/`skip` steps above a threshold (default: any), median latency above a threshold (default 20 s), and the degenerate patterns below (every run identical, every confidence 0). |

### Question-level checks (run once per sampled question)

| name | kind | what it flags |
| --- | --- | --- |
| `question_renders` | deterministic | Empty or whitespace-only question text; `options` present but unparseable as the format the method expects (`_parse_options` in `top_n`); multiple-choice type with fewer than two options or duplicate options; a sub-question whose parent text is empty; text over a length cap that would need scrolling inside the card. |
| `question_coherent` | judged | Shown exactly what the rater sees, in order: prefix, parent context, question text, options, suffix, and the rendering mode. Asked whether a rater could answer it: the question is a question, the options match the question, nothing reads as leaked metadata, a truncated passage, or an unrendered table. |
| `assistance_output` | deterministic | Method-specific. `top_n`: `parse_status` not `clean`, zero candidates, every candidate the same answer, `option_index` out of range for the row's options. `human_as_a_tool`: zero subtasks, every subtask confidence equal (especially 0), every subtask answer empty, `COMPLETE` on the first step with an empty synthesis answer, the pre-filled answers all above threshold with nothing delegated. Any method: step type `none` or `skip`, with the event-log `failure_reason` and detail. |
| `assistance_useful` | judged | Given the question and the first assistance step's payload, is the assistance about this question, plausible, and in the form the rater instructions describe. Catches a method that returns fluent nonsense without tripping a deterministic rule. |

The "every confidence is 0 and nothing is filled in" pattern from the brief is
`assistance_output` on a single question and `assistance_health` across the
sample. One question doing it is a warning; all of them doing it is an error.

## Sampling

- `sample.size` questions (default 15, allowed 1 to 50) drawn uniformly
  without replacement from the experiment's served rows (parent rows are
  header-only and skipped, as `question_count` skips them).
- Stratified only as far as "at least one of each `question_type`, and at
  least one sub-question when the upload has any", so a dataset with one odd
  row type is not missed by chance.
- The sample's question ids are stored on the run, so the report links each
  finding to a question and the admin can reopen exactly that question in the
  participant preview (`buildRaterPreviewUrl(experimentId, questionId)` already
  pins the first question served).
- `sample.seed` is null by default (fresh draw every run). Setting it makes
  runs comparable while iterating on prompts.

## Running assistance without a rater

The check calls the method the way `operations.start_assistance` does, but
not through it: that path needs a rater row, an activated assignment and a
session, and it writes `assistance_sessions` and `assistance_events` rows
that analytics would then have to filter out.

Instead `services/pilot_check/assistance.py`:

1. Resolves the method with `get_method(experiment.assistance_method)` and
   the params with `method.preparation_params(stored_params)`, the same
   resolution a real session records.
2. Calls `method.start(question, params, parent_question_text=...,
   experiment_system_prompt=experiment.system_prompt)`, bounded by a
   per-question timeout (default 120 s, the preparation runner's budget)
   under the existing `provider_slot()` limit so a check cannot starve live
   raters of provider slots.
3. Records the `InteractionStep` as returned: type, payload, `failure_reason`,
   `failure_detail`, latency. Multi-turn methods are only started; the
   check does not invent human input to advance them.

Nothing touches the assistance tables. The run's own table holds the steps.

A control arm (`assistance_method == "none"`) skips every assistance check
and says so in the report, rather than reporting "no assistance" as a
failure.

## Configuration file

`backend/pilot_check.toml`, checked in, loaded once at startup through
`Settings.pilot_check.config_path` (`PILOT_CHECK__CONFIG_PATH`, default that
file). Validation follows `_StrictModel`: an unknown check name or key fails
startup, so a typo cannot silently disable a check.

```toml
[sample]
size = 15
seed = null                       # set an int for repeatable draws

[judge]
# <provider>/<model-id>, same spelling as assistance_models entries.
model = "openrouter/anthropic/claude-sonnet-4-6"
temperature = 0

[assistance]
per_question_timeout_seconds = 120
# assistance_health thresholds
max_failure_share = 0.0           # any none/skip step is a finding
max_median_latency_seconds = 20

[gate]
# "warn":  launch asks for acknowledgement when the latest run is missing,
#          stale, or has findings at or above gate.severity.
# "block": launch refuses in those cases (no acknowledgement path).
# "off":   launch ignores the check (the UI still shows it).
mode = "warn"
severity = "warning"

# Every check must be listed. severity is "error", "warning" or "info".
[checks.consent_configured]
enabled = true
severity = "error"

[checks.launch_blockers]
enabled = true
severity = "error"

[checks.instructions_coherent]
enabled = true
severity = "warning"

[checks.markdown_mode]
enabled = true
severity = "warning"

[checks.question_renders]
enabled = true
severity = "error"

[checks.question_coherent]
enabled = true
severity = "warning"

[checks.assistance_output]
enabled = true
severity = "error"

[checks.assistance_health]
enabled = true
severity = "error"

[checks.assistance_useful]
enabled = true
severity = "warning"
```

Judged checks carry their rubric in code next to the check class, not in the
config file, so a prompt edit is a reviewed change with a test. The config
hash is stored on each run, so changing the file makes earlier runs stale
(below).

Per-experiment overrides are deliberately out of scope for the first
version. The file is one place for the team's standard; if an arm needs a
check off, that is a conversation, not a toggle.

## Data model

One new table, `pilot_check_runs`, one row per run:

| column | meaning |
| --- | --- |
| `id`, `experiment_id` (FK, CASCADE), `created_at`, `finished_at` | |
| `status` | `running`, `passed`, `warnings`, `failed`, `aborted` |
| `requested_by` | admin email from the session cookie |
| `fingerprint` | SHA-256 of the inputs the run saw (below) |
| `config_sha256` | hash of the config file as loaded |
| `sample_question_ids` | JSON array |
| `findings` | JSON array of `{check, severity, question_id?, message, detail?}` |
| `assistance_steps` | JSON array of `{question_id, step_type, payload, failure_reason, failure_detail, latency_ms}`, null for control arms |
| `judge_model` | the model id used, null if no judged check ran |
| `error` | exception text when `aborted` |

`status` is the worst severity among findings from enabled checks: no
findings is `passed`, worst `warning` or `info` is `warnings`, any `error`
is `failed`. The gate reads `status`, the fingerprint and `gate.severity`.

The `fingerprint` covers everything the check looked at and that an admin
can still change on a DRAFT: `description`, `system_prompt`,
`human_prompt_prefix`, `human_prompt_suffix`, `is_markdown`,
`session_duration_minutes`, `assistance_method`, resolved `assistance_params`,
the latest `uploads.id` and the question count, and `config_sha256`. The
experiment read recomputes it and reports the latest run as `stale` when it
differs. After LAUNCH the row is frozen, so a run made before the first main
round stays valid for later rounds.

The consent statement is not in the fingerprint: reading it is a live call
to the terms source, too slow for the experiment list. The run stores what
it saw in `terms_ref` (`label vN:sha256`) so a version bump is visible on
the run detail, and `consent_configured` re-reads it on every run.

The latest run is summarised onto `ExperimentResponse` (through the shared
`_build_experiment_responses` enrichment, like `needs_attention`):

```json
"pilot_check": {
  "state": "not_run" | "running" | "stale" | "passed" | "warnings" | "failed" | "aborted",
  "run_id": 12,
  "finished_at": "...",
  "counts": {"error": 0, "warning": 2, "info": 1},
  "gate_mode": "warn"
}
```

## API

All under the admin session.

- `POST /api/admin/experiments/{id}/pilot-check` starts a run and returns
  its row with `status: running`. 409 if one is already running for the
  experiment. 400 on a FINISHED experiment.
- `GET /api/admin/experiments/{id}/pilot-check` returns the latest run, or
  404 when none exists.
- `GET /api/admin/pilot-checks/{run_id}` returns one run in full, findings
  and assistance steps decoded.

The run executes in the API process as an `asyncio` task started by the
POST handler, with its own short-lived DB session (the pattern the
preparation workers use), and a whole-run deadline of
`sample.size × per_question_timeout + 60 s`. A `running` row older than that
deadline is reported as `aborted` with "process restarted" and can be
re-run. This is simpler than a durable claim in the preparation queue and
enough for a job an admin starts by hand and watches; if a check ever needs
to survive a deploy it can move onto `PreparationRunner`.

The UI polls the GET every 3 s while `running`. The row carries where the
run is: `stage` (`sampling` → `assistance` → `checks` → one stage per judged
check, named after it: `instructions_coherent`, `question_coherent`,
`assistance_useful` → `done`), `stage_detail` (a short line such as "9 of 15
questions probed" or "9 of 15 questions reviewed", updated after every model
call) and `stages`, the list of stages this particular run goes through (no
`assistance` or `assistance_useful` on a control arm, no judged stages when
the judge has no key). The deterministic checks share one stage because they
finish before a poll could show progress; the judged ones each get a dot
because they are where a run spends its time. The card draws them as a
horizontal stepper: finished stages light up with a glow, the current one
pulses, and the last dot takes the outcome's colour when the run lands. An
aborted run keeps the stage it died in, shown in red.

### Launch gate

`assert_launch_ready` stays as it is: hard blockers remain hard. A new
`assert_pilot_checked(experiment, db, *, acknowledged: bool)` runs after it
in `run_pilot_study`, `launch_from_card` and `run_experiment_round`:

- `gate.mode == "off"`: returns.
- Latest run `passed`, or `warnings` below `gate.severity`, and not stale:
  returns.
- Otherwise, with `mode == "warn"`: 409 with
  `{"code": "pilot_check", "state": ..., "run_id": ..., "counts": ...}`
  unless the request carries `acknowledge_pilot_check: true`, in which case
  the acknowledgement is written onto the round's row (`pilot_check_run_id`,
  `pilot_check_acknowledged_by`) and the launch proceeds.
- With `mode == "block"`: 409 regardless.

The acknowledgement columns on `experiment_rounds` make "who launched past a
failing check" answerable later without reading logs.

## UI

Experiment detail page, `ExperimentDetail.tsx`:

- **Overview tab.** The setup checklist gets a line under the four steps:
  "Pilot check: not run / running / 2 warnings / passed 3 h ago / stale
  since the instructions changed", with a button that jumps to the Launch
  tab. Not a fifth step: it is a verification of the steps, and the stepper
  semantics in `StepperTabs.tsx` are about configuration.
- **Launch tab.** A `PilotCheckCard` (`experiment-detail/PilotCheckCard.tsx`)
  sits above the one-click panel and the pilot form:
  - header row: state pill, "Run check" / "Re-run" button (disabled while
    running, with elapsed time), last-run time and who ran it;
  - a `Banner` in warn tone when `not_run` or `stale`, danger tone when
    `failed`, with one sentence saying what the state means;
  - findings grouped by check, severity first, each with its message and,
    for question-level findings, an "Open in preview" link that uses the
    existing pinned preview URL;
  - a collapsible "What the AI saw" per question-level assistance finding
    showing the step payload, so a 0-confidence subtask list is visible
    without a database query.
- **Launch buttons** (the one-click button, the pilot form submit, the
  recommendation card's "Launch round"). When the summary state is anything
  but `passed`/acceptable `warnings`, the click opens the existing
  `ConfirmDialog` with the state and counts and two actions: "Run the check
  first" (starts it and stays) and "Launch anyway" (retries with
  `acknowledge_pilot_check: true`). The dialog copy comes from the 409 body
  so the backend and the UI cannot disagree about why. In `block` mode the
  dialog has only the first action.
- **Experiment list** (`AdminView.tsx`). The existing attention dot gains a
  reason "Pilot check not run" for DRAFT experiments with questions and no
  run, so the nudge is visible before anyone opens the experiment.

`api.ts` gains `runPilotCheck`, `getPilotCheck`, `getPilotCheckRun`, and the
three launch calls accept the acknowledgement flag.

## Code layout

```
backend/pilot_check.toml
backend/services/pilot_check/
  __init__.py
  config.py        PilotCheckConfig (pydantic, strict), load_config()
  runs.py          start_run(), run_check() task body, fingerprint(), summarise()
  sampling.py      sample_questions()
  rater_view.py    the strings a rater receives for one question, built from
                   the same helpers the rater router uses
  assistance.py    run_method_once()
  judge.py         judge(rubric, material, schema) over services.assistance.llm.complete
  checks/
    base.py        Check, Finding, CheckContext (experiment, terms, sample,
                   rater views, assistance steps, config)
    registry.py    name -> class, mirrors services/assistance/registry.py
    consent.py, launch_blockers.py, instructions.py, markdown_mode.py,
    question_renders.py, question_coherent.py, assistance_output.py,
    assistance_health.py, assistance_useful.py
backend/routers/admin.py              three routes
backend/services/admin/status.py      assert_pilot_checked
backend/alembic/versions/..._add_pilot_check_runs.py
frontend/src/components/experiment-detail/PilotCheckCard.tsx
```

`rater_view.py` is the piece that keeps the check honest: it must build the
question material from the same functions the rater endpoints use
(`QuestionResponse` fields, `terms_for_rater`, `get_rater_instructions`,
`to_prolific_html`), so a change to what raters see changes what the check
sees. A test asserts the two agree for a fixture experiment.

## Cost and time

Per run, with defaults and an assisted arm:

| calls | count |
| --- | --- |
| assistance `start()` | 15 (human_as_a_tool: decomposition + confidence each) |
| judge, question-level | 30 (coherence + usefulness) |
| judge, experiment-level | 1 |

Roughly a minute wall-clock under the 8-slot provider limit, and well under
a dollar on the default judge. A control arm is 16 judge calls and no
assistance calls.

## Testing

- Unit: each check against hand-built `CheckContext`s (a 0-confidence
  subtask list, a `no_candidates` top-N step, a placeholder consent, a
  Markdown description with `is_markdown` false). The judge is a fake that
  returns a canned JSON verdict. Config loading rejects an unknown check
  name and a missing one.
- E2E (`make test`): POST starts a run and the GET reaches a terminal state
  with the provider client stubbed (the `no_provider_calls` fixture makes
  every assistance call fail, which is itself a good fixture: the run must
  report `assistance_health` as failed, not crash); the launch gate returns
  409 with the structured body and proceeds with the acknowledgement;
  fingerprint changes mark a run stale; a FINISHED experiment refuses a run.
- Playwright: the launch tab shows the "not run" banner and the run button;
  launching opens the confirm dialog; a mocked `passed` run hides it.

## Rollout

Three PRs, in order:

1. **Engine and API.** Config file and loader, the runs table and migration,
   sampling, `rater_view`, the deterministic checks, the three routes, the
   `pilot_check` summary on `ExperimentResponse`. Gate mode defaults to
   `off` in this PR so nothing changes for launches.
2. **Judged checks.** `judge.py`, the three judged checks and their rubrics,
   the skip-without-key path.
3. **UI and gate.** `PilotCheckCard`, the overview line, the list reason, the
   confirm dialog, `assert_pilot_checked`, the acknowledgement columns, and
   `gate.mode` flipped to `warn`. Admin guide section 4 gets a "Run the pilot
   check" paragraph.

## Implementation notes

Where the build departed from the design above:

- One PR rather than three, with the gate shipped in `warn` mode. The e2e
  suite turns the gate off by default (`pilot_check_gate_off` in
  `tests/e2e/conftest.py`) and the gate tests turn it back on.
- The 409 body is `{"detail": {"code", "message", "pilot_check"}}`. The
  frontend's `ApiError` keeps the parsed `detail`, and `pilotCheckRefusal`
  recognises the gate's refusal so `usePilotCheckGate` can show the dialog
  and retry with `acknowledge_pilot_check`.
- `ExperimentRoundResponse` exposes `pilot_check_run_id` and
  `pilot_check_acknowledged_by`, so the acknowledgement is readable from the
  API, not only the table.
- The experiment list's attention dot reads "Run the pilot check before
  launching." for a DRAFT with questions, no rounds, and a summary that
  needs acknowledgement; an unpublished round draft still wins.
- The judge steps down from a JSON schema to JSON mode to plain text when a
  provider rejects the format, and parses the reply the same way.
- `question_renders` parses options with the rater card's own rules
  (`|`, labelled items, lines, commas) rather than importing top-N's parser.
- The run's whole-run deadline also budgets two judge calls per question at
  the provider timeout with retries, since judged checks run sequentially.

## Open questions

- Should the acknowledgement require a free-text reason? Cheap to add, and
  it makes the audit column useful; it also adds friction to a path that is
  meant to stay available. Default here: no reason, just the identity.
- `sample.size` of 15 is a guess between the 10 and 20 in the brief. The
  cost table above says 20 is fine too; 15 keeps a run around a minute.
- Whether the check should also run `advance()` for `human_as_a_tool` with
  a synthetic "I don't know" answer to see the synthesis step. It would catch
  an empty-synthesis bug the first step cannot show, at the cost of
  inventing rater input. Left out of the first version; the per-method
  `assistance_output` rules can grow a `probe_advance` hook later.
- Whether a check run should be required again after each wave's upload on
  a duplicated experiment. The fingerprint already covers the latest upload
  id, so the answer falls out as "yes, it goes stale".
