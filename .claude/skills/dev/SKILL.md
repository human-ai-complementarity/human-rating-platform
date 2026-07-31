---
name: dev
description: Start, stop, restart, or check all local dev servers (backend Docker stack + frontend Vite) with one command each. Use when asked to bring up / tear down / restart the app or the servers, or when the UI or API stops responding.
---

The local app is two pieces:

- **Backend + DB** — Postgres, an Alembic migrate step, and the FastAPI API, all in Docker Compose. API on `:8000`, Postgres on `:5432`.
- **Frontend** — the Vite dev server on `:5173`, a plain Node process (not in Docker).

`make up` starts **only** the backend; the frontend is separate. The `dev.*`
targets below manage both at once.

## Commands (run from the repo root)

```bash
make dev.up        # start EVERYTHING: backend + DB (Docker) + frontend (background)
make dev.down      # stop EVERYTHING: frontend + backend + DB
make dev.restart   # dev.down then dev.up
make dev.status    # show what's running (compose services + frontend port)
```

Open the app at http://localhost:5173. API health: http://localhost:8000/api/health.

Underlying single-piece targets, if you need just one:

- `make up` / `make down` — backend + DB only (Docker).
- `make web` — frontend in the **foreground** (logs in the terminal; Ctrl+C to stop).
- `make dev.up` runs the frontend **detached**, logging to `frontend/vite.log`.

## Running these as an agent (important)

`make dev.up` and `make dev.restart` launch the frontend as a detached
background process, so under a tool that waits for completion they will **not
return** until the frontend later stops — they get moved to the background. That
is expected.

- Start them with `run_in_background: true` (or let them move to background).
- **Do not** `TaskStop` the `dev.up`/`dev.restart` task to "clean up" — the
  frontend can share its process group, so killing the task kills the frontend.
  Stop servers with `make dev.down` instead.
- To confirm readiness, poll rather than reading the make output:
  ```bash
  until curl -sf http://localhost:8000/api/health >/dev/null \
     && curl -sf http://localhost:5173/ >/dev/null; do sleep 2; done
  ```
  or run `make dev.status` as a separate command.

## Troubleshooting

- **UI not loading but API is fine:** the frontend isn't running (a bare `make
  up` doesn't start it). Run `make dev.up` or `make web`.
- **Port already in use (`:5173`/`:8000`):** something's still up. `make
  dev.down`, or `make dev.status` to see what. The frontend starts with
  `--strictPort`, so it fails loudly instead of drifting to another port.
- **Frontend logs:** `frontend/vite.log` (only when started detached via
  `dev.up`). For the backend, `make logs`.
- **Changed `backend/.env`?** The api container reads `.env` as an `env_file`
  at creation, so a running container won't pick up edits. Recreate it:
  `docker compose up -d --force-recreate api` (or `make dev.restart`).
- **Docker not running:** the backend targets need Docker. Start Docker Desktop
  first.
