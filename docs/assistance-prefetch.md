# Assistance preparation

This stack adds preparation in five reviewable stages:

1. Optional method contract and first-step adapter (this change).
2. Durable, fenced execution with bounded in-process workers.
3. Server-owned active question and one reserved successor.
4. A frontend queue owner that activates before display.
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

No background work is scheduled by the contract alone. Existing foreground
behavior remains unchanged at this stage.

## Durable execution

The API lifespan owns three small workers. One takes only foreground demand;
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

## Authoritative queue

`POST /raters/queue` reserves at most two questions: one active question and one
successor. Activation is an explicit revision-checked mutation. The existing
experiment assignment lock serializes selection; a rater-row lock serializes
queue mutation, submission, reset, and end. Selection retains the existing
coverage tiers and includes the active question's parent when choosing a sibling.
Preview reservations do not count toward real participant coverage.

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
visible until an identical retry is acknowledged. Assistance transport recovery retries frozen input with its original step revision.
The server returns an already committed step or reports that its claim is still
active. A turn claim uses the existing assistance-session row and the same bounded
execution/expiry budgets as preparation. Provider calls run outside transactions;
publication rechecks ownership and session validity. Submission conflicts offer
an explicit refresh from saved progress without overwriting an accepted answer.
New sessions use the queue protocol at depth one by default. The experiment
allowlist enables only the second slot and speculation. Stored legacy sessions
retain their original path until they drain.
