# Pilot check: an AI spot check before a study goes to raters

An admin-triggered check that samples an experiment's questions, walks each
one through the same code path a rater hits, runs the configured assistance
method on it, and reports anything a researcher would want to fix before
paying Prolific participants to look at it. The experiment read says whether the check has been run and what it
found.

Code: `backend/services/pilot_check/`, settings under `[pilot_check]` in
`backend/config.toml`.

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

Each check is one function with a stable name, a label, and a default
severity (`services/pilot_check/checks/`). The checks here read platform
state and apply rules; they need no model.

### Experiment-level checks (run once per run)

| name | what it flags |
| --- | --- |
| `consent_configured` | `TERMS__SOURCE_URL` unset (placeholder statement would be shown), source unreadable (the 503 raters would get), unknown placeholders left in the rendered text, rendered statement missing the study name or session length. The study flags' own rules: a sensitive or explicit study with no content warning details, the pinned statement not the one the flags select (standard vs warned), a deception study with no debrief, and the warned statement missing the promised details. |
| `markdown_mode` | The prefix or suffix contains Markdown structure (headings, lists, fenced code, links); the rater card shows them as plain text in both modes. `is_markdown` is false but sampled question text contains Markdown structure, or it is true and the text contains raw HTML. Stray `{{placeholders}}` anywhere. |
| `assistance_health` | Aggregate over the sampled assistance runs: any `none`/`skip` steps, median latency above `max_median_latency_seconds` (default 20 s), and every run byte-identical. |

### Question-level checks (run once per sampled question)

| name | what it flags |
| --- | --- |
| `question_renders` | Empty or whitespace-only question text; a multiple-choice question with no options or only one (parsed with top-N's `parse_options`, the rules the rater card uses); duplicate options; a sub-question whose parent text is empty; text over 20,000 characters, which needs scrolling inside the card. |
| `assistance_output` | Method-specific. `top_n`: zero candidates, every candidate the same answer, every confidence 0, no rationales. `human_as_a_tool`: zero subtasks, every subtask confidence equal (especially 0), every subtask answer empty, `COMPLETE` on the first step with an empty synthesis answer, the pre-filled answers all above threshold with nothing delegated. Any method: step type `none` or `skip`, with the `failure_reason` and detail. |

The "every confidence is 0 and nothing is filled in" pattern from the brief is
`assistance_output` on a single question and `assistance_health` across the
sample. One question doing it is a warning; all of them doing it is an error.

The launch gate's own blockers are not repeated as a check: the fingerprint
covers every blocker input, so a run made with a blocker present is already
stale by the time it could matter.

## Settings

`[pilot_check]` in `config.toml`, overridable as `PILOT_CHECK__<KEY>`:

| key | default | meaning |
| --- | --- | --- |
| `sample_size` | 15 | served questions drawn per run (1 to 50) |
| `max_median_latency_seconds` | 20 | `assistance_health` latency threshold |
| `severities` | `{}` | per-check overrides of the default severity, e.g. `markdown_mode = "info"` |

A SHA-256 of these values is stored on every run and is part of the
fingerprint, so changing one marks earlier runs stale. Which checks run is
not configurable: a check that should not run is a conversation about the
check, not a toggle.

## Sampling

- `sample_size` questions drawn uniformly without replacement from the
  experiment's served rows (parent rows are header-only and skipped, as
  `question_count` skips them).
- Stratified only as far as "at least one of each `question_type`, and at
  least one sub-question when the upload has any", so a dataset with one odd
  row type is not missed by chance.
- The sample's question ids are stored on the run, so the report links each
  finding to a question.

## Running assistance without a rater

`operations.start_assistance` needs a rater row, an activated assignment and
a session, and it writes `assistance_sessions` and `assistance_events` rows
that analytics would then have to filter out. The check goes through the
same `runner.start_method` call that path uses (the runner's timeout, a
`provider_context` so the provider events are attributed, any failure
degraded to a `none` step) but keeps the result on the run:

1. Resolves the method with `get_method(experiment.assistance_method)` and
   the params with `method.preparation_params(stored_params)`, the same
   resolution a real session records.
2. Calls `start_method(...)` for each sampled question, four at a time under
   the existing `provider_slot()` limit so a check cannot starve live raters
   of provider slots.
3. Records the `InteractionStep` as returned: type, payload, `failure_reason`,
   `failure_detail`, latency. Multi-turn methods are only started; the
   check does not invent human input to advance them.

A control arm (`assistance_method == "none"`) skips every assistance check
and says so in the report, rather than reporting "no assistance" as a
failure.

## Data model

One table, `pilot_check_runs`, one row per run (columns documented on the
model in `models.py`). `status` is the worst severity among findings: none,
or only `info`, is `passed`; worst `warning` is `warnings`; any `error` is
`failed`; `aborted` is a run that died, with `error` saying why. A check
that crashes, or a judge call that fails, is recorded as a `warning` so a
check that never ran cannot read as passed; a judge skipped for want of a
key is `info`.

The `fingerprint` covers everything the check looked at and that an admin
can still change on a DRAFT: `description`, `system_prompt`,
`human_prompt_prefix`, `human_prompt_suffix`, `is_markdown`, `render_math`,
`session_duration_minutes`, the study flags and content warning details,
`assistance_method`, resolved `assistance_params`, the latest `uploads.id`
and the question count, and the settings hash. The experiment read
recomputes it and reports the latest run as `stale` when it differs. The
pinned statement ids are left out on purpose: the first publish writes them,
and a run made on the draft must stay valid for the rounds that follow. A
flag changed after the pin is still caught, by the flags in the fingerprint
and by `consent_configured` comparing the pin to the flags on each run.

The consent statement text is not in the fingerprint: reading it is a live
call to the terms source, too slow for the experiment list. The run stores
what it saw in `terms_ref` (`label vN:sha256`) so a version bump is visible
on the run detail, and `consent_configured` re-reads it on every run.

The latest run is summarised onto `ExperimentResponse.pilot_check` (`state`,
`run_id`, `counts`, `reason`) by `summary.summarise`.

## API

All under the admin session.

- `POST /api/admin/experiments/{id}/pilot-check` starts a run and returns
  its row with `status: running`. 409 if one is already running for the
  experiment. 400 on a FINISHED experiment or one with no questions.
- `GET /api/admin/experiments/{id}/pilot-check` returns the latest run, or
  404 when none exists.

The run executes in the API process as an `asyncio` task started by the
POST handler, with its own short-lived DB sessions. It touches the row's
`updated_at` after every step; a `running` row left untouched for five
minutes belongs to a process that is gone, reads as `aborted`, and can be
re-run. This is simpler than a durable claim in the preparation queue and
enough for a job an admin starts by hand and watches.

A client polls the GET while `running`. The row carries where the run is:
`stage` (`sampling`, `assistance`, `checks`, `done`) and `stage_detail` (a
short line such as "9 of 15 questions probed", updated after every probe).
The response adds `stages`, the labelled list of stages a run of this
experiment goes through (no `assistance` on a control arm), computed at read
time.

## Testing

- Unit (`tests/unit/test_pilot_check_units.py`): each check against
  hand-built `CheckContext`s; the summary and fingerprint.
- E2E (`tests/e2e/test_pilot_check.py`, under `make test`): a run starts and
  lands with the provider client stubbed (the `no_provider_calls` fixture
  makes every assistance call fail, which is itself a good fixture: the run
  must report `assistance_health` as failed, not crash); a placeholder
  consent fails it; a stale fingerprint; a second run while one is running;
  a run that stopped reporting progress.

## Open questions

- `sample_size` of 15 is a guess between the 10 and 20 in the brief; 15
  keeps a run around a minute once the model-backed checks are in.
- Whether the check should also run `advance()` for `human_as_a_tool` with
  a synthetic "I don't know" answer to see the synthesis step. It would catch
  an empty-synthesis bug the first step cannot show, at the cost of
  inventing rater input. Left out; the per-method `assistance_output` rules
  can grow a `probe_advance` hook later.
