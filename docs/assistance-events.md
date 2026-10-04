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
| `step_type`             | response: the step produced; request: the step being answered (null on the opening start) |
| `status`                | `ok`, `error` (RuntimeError or a method-reported `failure_reason`), `timeout` (TimeoutError) |
| `latency_ms`            | wall-clock duration of the method call; response rows only              |
| `payload`               | JSON. request: `{"params"}` on start, `{"human_input"}` on advance. response: `{"payload", "state", "is_terminal", "failure_reason"?}` |
| `error`                 | exception text or `failure_reason`; null on success                     |

Each `start` or `advance` writes exactly two rows, in the same transaction as
the session update. Resuming an open session (a second `start` for the same
question) writes nothing; nothing crossed the method boundary.

A `none` or `skip` session is retried on the rater's next visit. The session
row is reused rather than deleted so the failed attempt's rows stay attached;
the retry adds its own pair.

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
