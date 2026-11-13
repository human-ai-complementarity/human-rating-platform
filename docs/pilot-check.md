# Pilot check: an AI spot check before a study goes to raters

A check that samples an experiment's questions, walks each one through the
same code path a rater hits, runs the configured assistance method on it,
and reports anything a researcher would want to fix before paying Prolific
participants to look at it. It starts on its own when a question upload
lands, and an admin can start it by hand. The experiment and launch pages
say whether the check has been run, show its findings, and ask before
creating a study without a clean run. They ask; they never refuse. A
judge will always find something to fix, so the decision stays with the
admin and the check records who made it.

Code: `backend/services/pilot_check/`, settings under `[pilot_check]` in
`backend/config.toml`, UI in `frontend/src/components/experiment-detail/`
(`PilotCheckCard.tsx`, `usePilotCheckGate.tsx`).

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
severity (`services/pilot_check/checks/`). There are two kinds:

- **Deterministic** checks read platform state and apply rules. They need no
  model and always run.
- **Judged** checks hand the rater-facing material to an LLM judge with a
  rubric and turn its JSON answer into findings. They are skipped, with a
  visible "skipped: no judge key" finding, when the judge model's provider
  has no key on the server.

### Experiment-level checks (run once per run)

| name | kind | what it flags |
| --- | --- | --- |
| `consent_configured` | deterministic | `TERMS__SOURCE_URL` unset (placeholder statement would be shown), source unreadable (the 503 raters would get), unknown placeholders left in the rendered text, rendered statement missing the study name or session length. The study flags' own rules: a sensitive or explicit study with no content warning details, the pinned statement not the one the flags select (standard vs warned), a deception study with no debrief, and the warned statement missing the promised details. |
| `instructions_coherent` | judged | The rater instructions (`description`, as rendered on the intro screen) read as instructions for the task the questions pose: they name what to rate and how, they do not contradict the prompt prefix/suffix, and they do not mention an assistance method that is off (or omit one that is on). The judge sees the instructions, the prefix/suffix, the method's `rater_instructions`, and three sampled questions for context. |
| `markdown_mode` | deterministic | The prefix or suffix contains Markdown structure (headings, lists, fenced code, links); the rater card shows them as plain text in both modes. `is_markdown` is false but sampled question text contains Markdown structure, or it is true and the text contains raw HTML. Stray `{{placeholders}}` anywhere. |
| `assistance_health` | deterministic | Aggregate over the sampled assistance runs: any `none`/`skip` steps, median latency over 20 s, and every run byte-identical. |

### Question-level checks (run once per sampled question)

The judged ones (`question_coherent`, `assistance_useful`) end with one more
judge call when two or more questions were flagged: the per-question verdicts
go back to the judge, which merges them into distinct problems, each naming
the questions it applies to. A question the merge leaves out keeps its own
finding; if the call fails the per-question findings stand.

| name | kind | what it flags |
| --- | --- | --- |
| `question_renders` | deterministic | Empty or whitespace-only question text; a multiple-choice question with no options or only one (parsed with top-N's `parse_options`, the rules the rater card uses); duplicate options; a sub-question whose parent text is empty; text over 20,000 characters, which needs scrolling inside the card. |
| `question_coherent` | judged | Shown exactly what the rater sees, in order: prefix, parent context, question text, options, suffix, and the rendering mode. Asked whether a rater could answer it: the question is a question, the options match the question, nothing reads as leaked metadata, a truncated passage, or an unrendered table. |
| `assistance_output` | deterministic | Any method: step type `none` or `skip`, with the `failure_reason` and detail. `top_n`: zero candidates. `human_as_a_tool`: zero subtasks, no subtask with an AI answer filled in (the brief's "every confidence 0 and nothing filled in" shape), `COMPLETE` on the first step with an empty synthesis answer. Whether what came back is any good is `assistance_useful`'s call. |
| `assistance_useful` | judged | Given the question and the first assistance step's payload, is the assistance about this question, plausible, and in the form the rater instructions describe. Catches a method that returns fluent nonsense without tripping a deterministic rule. |

### Screenshots (the last stage)

`rater_screens` (judged) opens the rater page in headless Chromium on the
server (`services/pilot_check/screens.py`), in preview mode with the sampled
questions pinned by id: the consent screen, the intro when there is one, then
each question, waiting for the assistance to arrive on an assisted arm.
Nothing is answered. A vision judge reads each screenshot for what would
confuse, block or mislead a rater: raw Markdown or LaTeX, cut-off or
overlapping text, a missing image, an error, assistance that failed, options
that do not fit the question. A screen that never finished loading is a
finding too. Each finding names its screenshots, and only those are stored
(`pilot_check_screenshots`, the latest run of an experiment only); the card
links each one, served by `GET .../pilot-check/runs/{run_id}/screens/{screen}`.

It is skipped (info) when `rater_app_url` is empty, and a warning when the
server cannot start a browser. It reads the same screens the other checks
read as text, end to end; if its findings prove good enough, it can replace
some of them.

The launch gate's own blockers are not repeated as a check: the pilot gate
runs after `assert_launch_ready`, and the fingerprint covers every blocker
input, so a run made with a blocker present is already stale by the time it
could matter.

## Settings

`[pilot_check]` in `config.toml`, overridable as `PILOT_CHECK__<KEY>`:

| key | default | meaning |
| --- | --- | --- |
| `sample_size` | 15 | served questions drawn per run (1 to 50) |
| `judge_model` | `openrouter/anthropic/claude-sonnet-4-6` | `<provider>/<model-id>`, as in `assistance_models` |
| `gate_mode` | `warn` | `warn` asks for acknowledgement, `off` ignores the check (the UI still shows it) |
| `severities` | `{}` | per-check overrides of the default severity, e.g. `markdown_mode = "info"` |
| `auto_start` | `true` | start a run when an upload or an edit leaves the experiment ready to launch (see below) |
| `runner` | `inline` | `inline` runs checks in the API process; `worker` queues them for the worker process (see "Where runs happen") |
| `worker_concurrency` | 1 | runs one worker process takes on at once; the rest wait queued |
| `rater_app_url` | `""` | frontend origin the screenshot check opens, e.g. `https://platform.complementarities.org`; empty skips it |

The screenshot check needs Chromium where runs happen: the pilot check
worker in production. On Render, add `PLAYWRIGHT_BROWSERS_PATH=0` to the
worker's environment (the browser then lives inside the project's venv, which
the runtime keeps) and append `&& uv run --no-sync playwright install chromium`
to its build command. In the Docker dev stack (inline), install it into the
running api container once:
`docker compose exec api sh -c 'apt-get update && apt-get install -y libatomic1 && PLAYWRIGHT_BROWSERS_PATH=/uv-env/ms-playwright playwright install --with-deps chromium'`
with `PLAYWRIGHT_BROWSERS_PATH=/uv-env/ms-playwright` in the api environment.

A SHA-256 of these values, `gate_mode`, `auto_start`, `runner` and
`worker_concurrency` excepted (they change what a launch asks and when and
where a run happens, not what a run looked at), is
stored on every run and is part of the fingerprint, so changing one marks
earlier runs stale. Which checks run is not configurable: a check that
should not run is a conversation about the check, not a toggle.

## Starting on its own

The run takes about a minute; left to the admin, that minute is spent
waiting at the launch step. So the two admin writes that shape what a rater
sees, a question upload (`POST /api/admin/experiments/{id}/upload`) and an
experiment edit (`PATCH /api/admin/experiments/{id}`), start a run once they
have committed, with `requested_by` the admin who made the write, the same
identity the manual POST records. The inference pipeline creates the
experiment and then uploads an export that carries the instructions and the
model, so there the upload is the write that starts the run. On the website
the questions usually land before the instructions, so it is the edit that
fills them in. A write is skipped, with a log line saying why, when:

- `auto_start` is off;
- the experiment is FINISHED;
- the experiment is still missing something to fill in
  (`experiment_launch_blockers` in `services/admin/status.py`: questions,
  rater instructions, an internal name, and for an assisted arm a pinned
  model with a key). A run before that would report the gaps the launch gate
  already names, and be stale as soon as they are filled. The gate's testing
  preview blocker is not one of these: a preview is where the check is
  wanted, so a `preview-testing` draft is checked like any other;
- a run is in progress (its row touched within the last five minutes);
- the latest run is still fresh: its fingerprint matches the experiment as
  it now stands, so edits a run does not look at (the public name, tags)
  start nothing. An aborted run counts for nothing, whatever its fingerprint.

An edit that lands while a run is going is not seen by that run. When a run
finishes cleanly it asks the same question once more and starts a follow-up
if the experiment has moved on; the follow-up's fingerprint matches, so
there is no third. An aborted run starts nothing, or a failure that repeats
would loop.

The trigger never fails or delays the write: every exception is logged and
the response is returned as it was. Duplicating an experiment copies its
questions and uploads but does not start a run; the copy is usually edited
before launch, and the edit starts one.

## Where runs happen

A run makes dozens of LLM calls and, with the screenshot stage, holds a
browser. In the API process that serves raters, those calls take the same
provider slots and rate limit as raters' assistance, and the memory comes
out of the same instance. So in production `runner = "worker"`: starting a
run (the POST, or an upload or edit) only inserts it as `queued`, and a
separate process, `python -m pilot_check_worker` (`services/pilot_check/worker.py`,
a Render background worker), claims queued runs (`runs.claim_next`, `FOR UPDATE
SKIP LOCKED`, oldest first), at most `worker_concurrency` at a time, and runs
them exactly as the inline path does. The worker has its own OpenRouter key.

- A queued run reads as in progress everywhere: the card says so, a second
  start gets the 409, auto-start skips it, the launch gate asks.
- One left unclaimed for 30 minutes (no worker running) reads as aborted,
  with an error saying so, and can be started again.
- Stopping the worker (a deploy, SIGTERM) cancels its runs, which mark
  themselves aborted; the next change or a manual start runs them again.
- A run's follow-up (an edit that landed mid-run) is queued by the worker
  like any other.

`inline`, the default, keeps the old behaviour: the API runs the check as a
background task. Local development and the test suite use it.

## Sampling

- `sample_size` questions drawn uniformly without replacement from the
  experiment's served rows (parent rows are header-only and skipped, as
  `question_count` skips them).
- Stratified only as far as "at least one of each `question_type`, and at
  least one sub-question when the upload has any", so a dataset with one odd
  row type is not missed by chance.
- The sample's question ids are stored on the run, so the report links each
  finding to its questions, and the admin can reopen each in the participant
  preview (`buildRaterPreviewUrl(experimentId, questionId)` pins the first
  question served).
- A finding names every question it applies to (`question_ids`), and the run
  merges findings that say the same thing into one row, so a problem shared by
  nine questions is one line with nine links, not nine lines.

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
`run_id`, `counts`, `needs_acknowledgement`, `reason`) by
`summary.summarise`, the same function the launch gate reads, so the UI and the
409 cannot disagree.

## API

All under the admin session.

- `POST /api/admin/experiments/{id}/pilot-check` starts a run and returns
  its row with `status: running`. 409 if one is already running for the
  experiment. 400 on a FINISHED experiment or one with no questions.
- `GET /api/admin/experiments/{id}/pilot-check` returns the latest run, or
  `null` when none has been started.

The run executes in the API process as an `asyncio` task started by the
POST handler or by an upload, with its own short-lived DB sessions. It
touches the row's `updated_at` after every probe and every judge call; a
`running` row left untouched for five minutes belongs to a process that is
gone, reads as `aborted`, and can be re-run. A clean shutdown cancels the
tasks still going and marks their rows `aborted` before the database
closes. This is simpler than a durable claim in the preparation queue and
enough for a job an admin starts, or an upload starts, and watches.

The UI polls the GET every 3 s while `running`. The row carries where the
run is: `stage` (`sampling`, `assistance`, `checks`, then one stage per
judged check named after it, then `done`) and `stage_detail` (a short line
such as "9 of 15 questions probed", updated after every model call). The
response adds `stages`, the labelled list of stages a run of this experiment
goes through (no `assistance` or `assistance_useful` on a control arm, no
judged stages when the judge has no key), computed at read time. The
deterministic checks share one stage because they finish before a poll could
show progress; the judged ones each get a dot because they are where a run
spends its time.

### Launch gate

`assert_launch_ready` stays as it is: hard blockers remain hard.
`gate.gate_for` runs after it in `run_pilot_study` and
`run_experiment_round` (`launch_from_card` goes through `run_pilot_study`):

- `gate_mode == "off"`: returns.
- Latest run `passed` and not stale: returns.
- Latest run flagged but not stale, and a round was already launched against
  it: returns, carrying that round's acknowledger onto the new one. A flagged
  run is asked about once; only a change to the experiment asks again.
- Otherwise: 409 with
  `{"detail": {"code": "pilot_check", "message", "pilot_check": <summary>}}`
  unless the request carries `acknowledge_pilot_check: true`, in which case
  the acknowledgement is written onto the round's row (`pilot_check_run_id`,
  `pilot_check_acknowledged_by`, both on `ExperimentRoundResponse`) and the
  launch proceeds.

There is deliberately no mode that refuses an acknowledged launch. The check
is advisory: it surfaces what a rater would hit and who decided to go ahead
anyway, and leaves the decision to the admin.

The frontend's `ApiError` keeps the parsed `detail`; `usePilotCheckGate`
recognises the refusal, shows it in a dialog built from the 409's own body,
and retries the same call with the acknowledgement. The experiment list's
attention dot reads "Run the pilot check before launching." for a DRAFT with
questions, no rounds, and a summary that needs acknowledgement.

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

- Unit (`tests/unit/test_pilot_check_units.py`): each check against
  hand-built `CheckContext`s; the judge is a fake that returns a canned
  verdict; the summary, the fingerprint, and the auto-start skip rules.
- E2E (`tests/e2e/test_pilot_check.py`, under `make test`): a run starts and
  lands with the provider client stubbed (the `no_provider_calls` fixture
  makes every assistance call fail, which is itself a good fixture: the run
  must report `assistance_health` as failed, not crash); a stale fingerprint;
  a run that stopped reporting progress; the gate's 409 and acknowledgement;
  an upload or edit starting a run, and the follow-up after a mid-run edit.
  The suite runs with the gate off (`pilot_check_gate_off` in conftest) and
  `auto_start` off (`pilot_check_autostart_off`), since most tests set up
  experiments and never look at the check; the `pilot_check_gate` and
  `pilot_check_autostart` fixtures turn them back on.
- Playwright (`frontend/e2e/smoke.spec.ts`): the stepper mid-run and landed,
  the acknowledge dialog and its cancel path.

## Open questions

- Should the acknowledgement require a free-text reason? Cheap to add, and
  it makes the audit column useful; it also adds friction to a path that is
  meant to stay available. Default here: no reason, just the identity.
- `sample_size` of 15 is a guess between the 10 and 20 in the brief. The
  cost table above says 20 is fine too; 15 keeps a run around a minute.
