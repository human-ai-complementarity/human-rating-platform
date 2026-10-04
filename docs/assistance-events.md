# Assistance event log

`assistance_sessions` holds the *current* step of each rater/question
assistance interaction. `step_type`, `state` and `payload` are overwritten on
every advance, so once a multi-turn session ends there is no record of how it
got there.

`assistance_events` fixes that. It is append-only and written by
`services/assistance/operations.py` around every method call:

| column                  | meaning                                                                 |
| ----------------------- | ----------------------------------------------------------------------- |
| `assistance_session_id` | FK to `assistance_sessions` (CASCADE delete)                            |
| `created_at`            | when the row was written                                                |
| `direction`             | `request` (what went into the method) or `response` (what came out)     |
| `step_type`             | response: the step produced; request: the step being answered (null on a session's very first start; `none`/`skip` on a retry of a failed start) |
| `status`                | `ok`; `error` (any exception escaping the method, or a method-reported `failure_reason`); `timeout` (a `TimeoutError` escaping the method; note the shipped methods catch provider timeouts themselves and report them as `error` / `provider_error`) |
| `latency_ms`            | wall-clock duration of the method call; response rows only              |
| `payload`               | JSON. request: `{"params"}` on start, `{"human_input"}` on advance. response: `{"payload", "state", "is_terminal", "failure_reason"?}` |
| `error`                 | exception text or `failure_reason`; null on success                     |

Each `start` or `advance` writes exactly two rows, in the same transaction as
the session update. Resuming an open session (a second `start` for the same
question) writes nothing; nothing crossed the method boundary.

A `none` or `skip` session is retried on the rater's next visit. The session
row is reused rather than deleted so the failed attempt's rows stay attached;
the retry adds its own pair. Reusing the row forfeits the unique-constraint
guard a fresh insert had, so the retry takes a `SELECT ... FOR UPDATE` on it:
two overlapping retries (a double-click, a client retry) serialize, and the
second, finding the row's `turn` moved while it waited, reports the first's
outcome (success or another failure) without running the method or logging a
pair of its own.

Any exception escaping a method, not only `RuntimeError`, degrades to the
fallback step (`none` on start, `skip` on advance) and is logged as an `error`
response row. A 500 would roll back the very rows meant to explain the failure.

## Turns

`advance` locks its session row too, but a lock alone cannot tell a duplicate
submit from a genuine next-turn input with the same text. `assistance_sessions.turn`
counts the steps the method has produced for the session (1 after start,
failed attempts included). Every `AssistanceStepResponse` carries it, and the
client echoes it as `turn` on advance. After the lock:

- `turn` matches: the input is applied as normal.
- `turn` is one behind and `human_input` equals the last advance request's:
  a duplicate submit. The step that submit produced is returned; the method
  does not run and nothing is written.
- any other mismatch: 409. The rater's input is not applied, and the client is
  told rather than left thinking it was.

Clients that send no `turn` get the old behaviour.

## Reading it

Reconstruct a session in order:

```sql
SELECT created_at, direction, step_type, status, latency_ms, error, payload
  FROM assistance_events
 WHERE assistance_session_id = :id
 ORDER BY id;
```

The `response` rows' `payload.payload` is exactly what the rater was shown at
each turn, so reliance analysis can be computed from the event stream later
without a separate presented-candidates column.

## Admin API

Both endpoints are read-only and sit behind the admin session.

- `GET /api/admin/experiments/{experiment_id}/assistance-sessions` lists an
  experiment's sessions newest first with an `event_count` each. Filter with
  `rater_id`, `question_id` or `step_type` (`step_type=skip` finds the
  sessions that failed mid-way); page with `skip` and `limit`.
- `GET /api/admin/assistance-sessions/{session_id}` returns one session with
  its current `payload` and the full `events` list, oldest first. JSON
  columns are decoded, so `events[i].payload` is an object, not a string.
