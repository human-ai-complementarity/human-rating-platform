# Assistance preparation

This stack adds preparation in five reviewable stages:

1. Optional method contract and first-step adapter (this change).
2. Durable, fenced execution with bounded in-process workers.
3. Server-owned active question and one reserved successor.
4. A frontend assistance owner using server-selected questions.
5. Measurement, disabled-by-default rollout, and operating guidance.

## Extending a method

`AssistanceMethod` defaults to no preparation. Opt in by implementing:

- `plan_preparation(context)`: pure, deterministic, inexpensive; return a
  `PreparationSpec` with a stable name, version, and JSON input snapshot.
- `prepare(spec)`: compute one private JSON artifact. Work must be bounded,
  safe to abandon, and require no future human answer.
- `consume_preparation(spec, artifact)`: validate the artifact and return an
  interaction step. Partial artifacts may require foreground composition.
  Never silently regenerate a prepared artifact.

Use `InitialStepPreparation` when the entire first step can run early. It
calls the existing `start` implementation, preserving prompts, models,
randomization, scoring, and private interaction state. Top-N and Human-as-a-Tool
use this adapter. Human-as-a-Tool prepares only its initial decomposition and
confidence scoring; `advance` still requires actual human input.

Bump the preparation version whenever artifact or input interpretation changes.
The runtime owns identity, participant isolation, persistence, deadlines, and
concurrency. Methods do not choose shared cache keys. Snapshots deliberately
exclude ground truth and upload metadata. Private artifacts and method state
must never be returned as queue metadata.

The adapter preserves method logic. This runtime changes foreground execution:
eligible initial assistance uses the worker pool even before speculative prefetch
is enabled, and retries reuse durable results, including terminal NONE steps.

## Durable execution

The API lifespan owns eight workers by default, configurable from 2 to 64 with
`prefetch.worker_count` / `PREFETCH__WORKER_COUNT`. One takes only foreground demand;
others prefer foreground demand before speculative work. PostgreSQL owns claims
across processes. A claim lasts 195 seconds; computation has a 180-second budget.
A crashed claim can be recovered after expiry, at most twice per stage. A fresh
owner token fences every publication. This is not exactly-once provider billing:
a provider may finish work after its caller times out.

Preparation produces a private artifact. Consumption runs only after foreground
demand and has its own claim, so a partial-artifact method can do foreground
composition without racing duplicate starts. The existing assistance-session
uniqueness constraint remains the final publication guard. Terminal NONE results
are reused. Method defaults are resolved before taking the input snapshot.

All workers use independent, short-lived database sessions. Waiting HTTP requests
release their transaction. Shutdown cancels local tasks; durable claims recover
after expiry. Provider calls share an eight-slot per-process limit; speculation
uses at most four. Limits multiply with the number of API processes.

The feature does not yet reserve or speculatively execute future questions.

Input identity includes rater, question, session start, method name, preparation
version and serialized inputs. A method cannot accidentally share work between
questions by omitting a question identifier from its own inputs. Session reset
and end are checked again under a rater-row lock before publication.


Startup requires LISTEN to subscribe within 10 seconds. `/api/health` returns 503
while the listener is disconnected or a runner task has stopped. Waiting requests
fail with a retryable 503 when their listener disconnects; reconnect reestablishes
LISTEN before new waits read durable state. No database polling fallback is added.

Graceful shutdown cancels and awaits local workers, then spends at most five seconds
releasing only their still-owned claims. Prepared artifacts and attempt counts are
retained. Another process can resume immediately; hard crashes or failed cleanup
still use lease expiry. Cancellation does not guarantee that a provider avoided billing.

Provider-slot waits count against the 180-second execution budget; time waiting
unclaimed in the durable queue does not. Raising workers does not raise the shared
eight-provider-call limit (four speculative). Validate expected arrival bursts
before deployment, and inspect assistance waits and timeouts during the first study.
The default is a starting limit, not a measured production capacity guarantee.

## Authoritative queue

`GET /raters/next-question` serves one current question and reserves up to `k` successors.
Set `prefetch.lookahead_questions` (or `PREFETCH__LOOKAHEAD_QUESTIONS`) to `k`,
from 0 to 5, default 1. The bound limits assignment hoarding and speculative cost;
raise it only with measurements. Zero gives a queue-only baseline. Shrinking `k`
stops refills beyond the new bound; already reserved work drains without discarding
paid results. Speculation stops immediately at zero, subject to in-flight calls. Serving activates the selected head in the same transaction as selection. The existing
experiment assignment lock serializes selection; a rater-row lock serializes
queue mutation, submission, reset, and end. Selection retains the existing
coverage tiers and includes the active question's parent when choosing a sibling.
Preview reservations do not count toward real participant coverage.

Before first activation, a non-preview question is replaced only if its submitted,
non-preview ratings have reached the target and another eligible question remains
underfilled. The existing reservation-aware selector chooses among unfinished
questions, preserving parent continuity within those candidates. A queued
replacement retains its reservation and prepared work; otherwise the queue reserves
it normally. Releasing the old head cancels its preparation and fences late results.
The next-question response is authoritative: the browser displays and demands
only the returned question.

Smaller coverage differences, preview navigation, and already activated questions
do not trigger replacement. If no eligible unfinished work remains, the existing
completed-question policy stays in effect. This is a serving-time snapshot, not a
guarantee against another participant submitting immediately afterward. It trades
occasional unused preparation for better completion coverage without a new queue,
selection algorithm, or schema change.

New queue submissions carry assignment ID and generation. An exact retry returns
the existing rating; a conflicting answer returns 409. The old submission
contract remains unchanged for sessions that have not entered the queue protocol.
Legacy `/next-question` remains an object or null, including during queue rollout.

Only an activated question can start assistance or be submitted. Grace preserves
the active question and releases unactivated reservations. Ending releases all
reservations. Preview reset deletes assignments and invalidates older signed
session generations. Failed multi-turn SKIP results require an explicit skip
mutation and cannot reselect the same question in that session.

Set `prefetch.experiment_ids` (or `PREFETCH__EXPERIMENT_IDS` as a JSON array) to opt
experiments in. The default is empty. Removing an experiment stops new speculative
requests and limits further refills to one; existing active work and already
reserved work can drain through the same protocol.


### Server-owned scheduling

`/next-question` reserves, rechecks coverage, and activates the current question
under the existing assignment lock. It then persists preparation demand for
successors before returning, without waiting for provider computation. Preview
pins use the same serving path. A second fill after activation replenishes any
successor promoted by the coverage check.

Question responses carry assignment ID and generation for exact submission
retries. The browser does not reserve, activate, or prepare successors. Explicit
skip and client-visible wait telemetry remain separate actions. Legacy clients
may omit assignment identity; the server still requires their question to match
an active assignment once the session enters queue mode.

Legacy reservations inserted after the migration backfill are valid before
queue enrollment. Enrollment marks those already-served reservations active,
preventing assistance from getting stuck after a rolling deployment.
## Browser ownership

`useRaterQueue` owns reservation, activation, preparation requests, and assistance
responses. It accepts only responses for the current request generation and
ignores older queue revisions. Refills coalesce while retaining pending demand.
`AssistancePanel` renders the supplied resource and submits actual human input;
it no longer independently starts assistance. No hidden components do work.

Both fresh and restored sessions wait for intro acknowledgment before reserving
or preparing work. The hook activates a question before exposing it to the view,
then starts foreground assistance and requests preparation for the successor.
Submission transport failures freeze the exact request and keep the answer
visible until an identical retry is acknowledged. Assistance transport recovery retries frozen input with its original turn counter.
The server returns an already committed step or reports that its claim is still
active. A turn claim uses the existing assistance-session row and the same bounded
execution/expiry budgets as preparation. Provider calls run outside transactions;
publication rechecks ownership and session validity. Submission conflicts offer
an explicit refresh from saved progress without overwriting an accepted answer.
New sessions use the queue protocol at depth one by default. The experiment
allowlist enables only the second slot and speculation. Stored legacy sessions
retain their original path until they drain.

Initial assistance HTTP waits end after 90 seconds with a retryable 503; the
180-second execution and 195-second claim budgets remain independent. The browser
keeps the loading UI and reattaches to the same persisted job after network errors
or HTTP 502/503/504, with up to six total requests and 1/2/4/5/5-second backoffs.
Other errors surface immediately. Exhaustion exposes the existing manual retry;
changing question/session or leaving the view cancels requests and retry timers.
Human-input advances keep their existing explicit retry behavior.


The runtime reuses the existing `turn` counter and assistance event history.
Committed-turn retries must match the previous turn and its exact human input;
other mismatches return 409. A concurrent request also receives retryable 409
while the owner computes outside a transaction. There is no second assistance
revision counter. Accepted background starts append their history atomically with
session creation; their latency includes preparation and consumption, excluding
queue residence. Retries of durable results add no event or provider call.
