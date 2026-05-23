# Human Rating Platform

A web platform for collecting human ratings on LLM responses, designed for Prolific studies.

## How It Works

An admin creates an experiment, uploads a CSV of questions, and shares a study URL
with Prolific. Prolific participants (raters) open the link, rate questions one at a
time, and are redirected to Prolific's completion page when done. The admin monitors
progress, views analytics, and exports ratings as CSV.

The frontend is a React SPA. The backend is a FastAPI JSON API. They talk over `/api`.
PostgreSQL stores everything. Alembic manages schema migrations.

## Project Layout

```text
backend/              FastAPI app, models, services, migrations, tests
  routers/            API route handlers (admin, raters)
  services/           Business logic (admin/, rater/, assistance/)
    assistance/       Assistance method plugin system (base, registry, operations, methods/)
  alembic/            Migration config + versions
  scripts/            migrate.sh, predeploy.sh, seed_dev.py, config_check.py
  config.toml         Default local settings
frontend/             React + TypeScript + Vite SPA
  src/api.ts          API client (route map, request pipeline, error handling)
  src/components/     UI components (AdminView, RaterView, ExperimentDetail, etc.)
scripts/              CI/deploy scripts (deploy.sh, resolve_deploy_target.sh)
.github/workflows/    CI (main.yml) + deploy (deploy.yml)
docker-compose.yml    Local dev stack (db + api + test runner)
sample_questions.csv  Example CSV for testing uploads
```

## Stack

- **Backend:** FastAPI + SQLModel (async), served on `:8000`
- **Frontend:** React + TypeScript + Vite, served on `:5173`
- **DB:** PostgreSQL, managed by Alembic migrations
- **Config:** Pydantic Settings (`backend/config.toml` + `.env` + env vars)
- **Python tooling:** `uv` / `uvx`

---

## Authentication (Clerk + Allowlist)

This project uses Clerk for frontend identity and a backend HTTP‑only cookie for invite‑only admin access.

- Frontend (Clerk): The React app is wrapped with `ClerkProvider` and uses prebuilt components for sign‑in/sign‑up. Configure with a publishable key.
- Backend (allowlist + session): After Clerk sign‑in, the frontend calls `POST /api/admin/auth/login` with `Authorization: Bearer <Clerk JWT (template=admin)>`. The backend verifies the JWT against Clerk JWKS, enforces issuer and audience, extracts the email from claims, checks an env‑based allowlist, and issues a signed HTTP‑only cookie that unlocks `/api/admin/*` routes. Signing out clears the cookie.

### Cookie model

- Name: `HRP_SESSION_COOKIE` (default `hrp_session`)
- Scope: `Path=/`, `HttpOnly`; SameSite depends on env — `Lax` in local dev, `None; Secure` in production (`COOKIE_SECURE=true`).
- Contents: a compact HMAC‑signed token with `{ email, iat, exp }`. No server‑side store is required.
- Validation: The server validates the signature and enforces `exp` on every request (session expires server‑side regardless of browser cookie TTL) and re‑checks the email against `ADMIN_ALLOWLIST`.
- Usage: The frontend sends requests with `credentials: 'include'`, so the browser includes the cookie on `/api` calls.

### Clerk Configuration

- Backend env (nested keys as configured in `config.py`):
  - `CLERK__ISSUER`: your Clerk issuer URL (e.g., `https://<your-tenant>.clerk.accounts.dev`).
  - `CLERK__JWKS_URL`: `https://<your-tenant>.clerk.accounts.dev/.well-known/jwks.json`.
  - `CLERK__AUDIENCE`: audience string set in your Clerk JWT template (e.g., `human-rating-platform-admin-api`).
- Clerk JWT template (Dashboard → JWT Templates):
  - Name: `admin`
  - Claims JSON:
    ```json
    {
      "aud": "human-rating-platform-admin-api",
      "email": "{{user.primary_email_address}}"
    }
    ```
  - `aud` must match `CLERK__AUDIENCE`.
  - `iss` is added by Clerk automatically. Copy the Issuer and JWKS endpoint shown in the template UI into backend env.
- Frontend usage:
  - Retrieve token with `useAuth().getToken({ template: 'admin' })` and send `Authorization: Bearer <token>` to `/api/admin/auth/login`.

### Allowlist

- Controlled by env var `ADMIN_ALLOWLIST` (comma‑separated or JSON array of emails).
- If your email isn’t in the allowlist, admin login returns 403 and the UI shows a friendly explanation.

```bash
# Admin allowlist + session cookie
ADMIN_ALLOWLIST=alice@example.com,bob@example.com
APP_SECRET_KEY=please-change-me-to-a-long-random-string
HRP_SESSION_COOKIE=hrp_session
HRP_SESSION_MAX_AGE=604800
COOKIE_SECURE=false

# CORS for local dev
APP__CORS_ORIGINS=["http://localhost:5173","http://localhost:8000"]

# Database
DATABASE__URL=postgresql://postgres:postgres@localhost:5432/human_rating_platform
```

### Render deployment envs

Frontend (Render Static Site):

- `VITE_CLERK_PUBLISHABLE_KEY=YOUR_PUBLISHABLE_KEY`
- `VITE_API_HOST=https://<your-api-service>.onrender.com`  (origin only; no path)

Backend (Render Web Service):

- `DATABASE__URL=postgresql://<user>:<pass>@<host>:5432/<db>` (Render Postgres internal URL)
- `APP__CORS_ORIGINS=["https://<your-web-service>.onrender.com"]`
- `ADMIN_ALLOWLIST=alice@example.com,bob@example.com`
- `APP_SECRET_KEY=<long-random-string>`
- `COOKIE_SECURE=true`

Clerk Dashboard: add your Render web domain under Settings → Domains so Clerk is allowed to operate on that origin. 

## Quick Start (~3 minutes)

### Prerequisites

- Python 3.10+
- Node.js 20+
- Docker + Docker Compose (must be running)
- Clerk Publishable Key (from Clerk Dashboard → API Keys)

### 1) Initialize env files

```bash
make env.sync
```

Creates `backend/.env`, `frontend/.env`, and `frontend/.env.local` from templates. Then set:

- `frontend/.env.local`
  
  ```
  VITE_CLERK_PUBLISHABLE_KEY=YOUR_PUBLISHABLE_KEY
  ```

- `backend/.env`
  
  ```
  ADMIN_ALLOWLIST=you@example.com
  APP_SECRET_KEY=please-change-me-to-a-long-random-string
  APP__CORS_ORIGINS=["http://localhost:5173","http://localhost:8000"]
  # DATABASE__URL=postgresql://postgres:postgres@localhost:5432/human_rating_platform
  ```

### 2) Start backend + DB (Terminal A)

```bash
make up
```

This starts Postgres, runs migrations, and launches the API with hot reload.

### 3) Start frontend (Terminal B)

```bash
cd frontend
make up
```

### 4) Open the app

- **App:** http://localhost:5173
- **API docs (Swagger):** http://localhost:8000/docs
- **Health check:** http://localhost:8000/api/health

Sign in with a Clerk account whose email appears in `ADMIN_ALLOWLIST`. The admin panel at `/admin` becomes available and the backend issues an HTTP‑only session cookie that authorizes `/api/admin/*` endpoints. Signing out clears this cookie.

---

## Daily Commands

```bash
make up          # start db + migrations + api (hot reload)
make ps          # show service status
make logs        # follow db/api logs
make test        # characterization tests (real db + migrations)
make fmt         # format backend with ruff
make down        # stop stack
```

DB lifecycle:

```bash
make db.up       # apply migrations
make db.down     # rollback one revision (or set MIGRATION_REVISION=...)
make db.new MIGRATION_NAME=add_new_column
make db.reset    # destructive: wipe + rebuild from migrations
make db.clear    # destructive: wipe local postgres volume
make db.seed     # seed local data (disabled by default, see config.toml [seeding]; calls Prolific API in real mode)
```

---

## Migrations

SQLModels in `backend/models.py` are the **declarative source of truth** — they describe what the database schema should look like. Migrations are **incremental operations** that get the database from its current state to match the models.

This is the required workflow for schema changes:

### 1) Change models

Edit SQLModel definitions in `backend/models.py`. For new tables, add a new `SQLModel` class with `table=True`. For existing tables, modify the field definitions. Update related schemas and services as needed.

### 2) Generate migration

```bash
make db.new MIGRATION_NAME=describe_change
```

Creates a timestamped file in `backend/alembic/versions/`.

### 3) Review the migration file

Always review the generated SQL/ops before applying. Autogenerate is helpful but not infallible.

### 4) Apply + test

```bash
make db.up
make test
```

### Roll back / inspect

```bash
make db.down                          # rollback one revision
cd backend
sh scripts/migrate.sh current         # show current revision
sh scripts/migrate.sh history         # show full history
```

### If your local DB is stale or broken

```bash
make db.reset
```

> Migration history is squashed for v0. If you were on an older local schema, do `make db.reset` once.

---

## Configuration

Backend settings are loaded via `backend/config.py` (`get_settings()`), with this precedence:

1. Python init kwargs
2. Process env vars
3. `backend/.env`
4. `backend/config.toml`
5. Python defaults

Env keys use Pydantic's nested `__` delimiter for nested settings models:

- `APP__CORS_ORIGINS` — JSON array, e.g. `["http://localhost:5173","http://localhost:8000"]`
- `APP__LOG_LEVEL` — log verbosity: `DEBUG`, `INFO`, `WARNING`, or `ERROR` (default: `INFO`). Logs are emitted as structured JSON with OpenTelemetry-compatible field names (`timestamp`, `severity`, `body`, `attributes`).
- `DATABASE__URL` — Postgres connection string
- `EXPORTS__STREAM_BATCH_SIZE` — CSV export chunking (memory/throughput tradeoff)
- `TESTING__EXPORT_SEED_ROW_COUNT` — characterization test dataset volume
- `SEEDING__*` — local seed generation (`enabled`, `experiment_name`, `question_count`, etc.)
- `PROLIFIC__API_TOKEN` — Prolific API token (optional; enables automated study management)
- `PROLIFIC__PROJECT_ID` — Prolific project ID to create studies under. If unset, Prolific uses the API user's `current_project_id`, which can land studies in the wrong workspace on multi-workspace accounts. The project's workspace and currency are derived automatically.
- `APP__SITE_URL` — public frontend URL used to build Prolific study links (default: `http://localhost:5173`)

Top‑level convenience envs (not nested):

- `ADMIN_ALLOWLIST` — comma‑separated or JSON array of admin emails
- `APP_SECRET_KEY` — HMAC signer for the HTTP‑only admin session cookie
- `RATER_SESSION_SECRET_KEY` — dedicated HMAC signer for rater session tokens (falls back to `APP_SECRET_KEY` if unset)
- `RATER_SESSION_TTL_SECONDS` — TTL in seconds for rater session tokens (defaults to 3600 = 60 minutes; same as session duration)
- `HRP_SESSION_COOKIE`, `HRP_SESSION_MAX_AGE`, `COOKIE_SECURE` — cookie name/ttl/secure flag
 - `ADMIN_AUTH_ENABLED` — set to `false` to bypass admin auth in dev/tests

Frontend env (`frontend/.env`):

- `VITE_API_HOST` — optional API origin for cross-origin deployments
  - **Local dev (default):** empty → frontend uses same-origin `/api` via Vite proxy
  - **Render example:** `https://human-rating-platform-api-uxnt.onrender.com`

## End-To-End Testing

This project uses three complementary end-to-end testing layers:

- `make test` runs the backend characterization suite against a real Postgres database with Alembic migrations applied. It exercises the API and service layer, including experiment creation, CSV uploads, rater sessions, analytics, exports, and Prolific study management with mocked Prolific HTTP responses.
- `cd frontend && npm run test:e2e` runs Playwright browser smoke tests against the local frontend. These tests mock `/api` responses and verify key admin and rater flows in the UI.
- A small number of Prolific-facing checks remain manual because they depend on an external platform, human participation, and spending real money.

Together, these layers provide good coverage of the local application behavior while keeping external dependencies out of automated test runs.

### Browser Smoke Coverage

The Playwright suite starts the frontend with `VITE_E2E_BYPASS_AUTH=true`. This bypasses Clerk only for the smoke harness so the tests can focus on application behavior without changing the production authentication flow.

The current browser smoke tests verify:

- creating an experiment from the admin dashboard
- navigating from experiment creation to the detail page
- uploading a CSV and seeing the upload summary update
- showing the pilot form before any Prolific rounds exist
- creating a pilot round and rendering the resulting round history
- showing recommendation data after the pilot flow returns it
- limiting the publish action to the currently linked unpublished round
- launching a follow-on round and appending it to round history
- opening the preview participant flow with `preview=true`
- updating export and stats requests when include-preview is enabled

### Manual Prolific Checklist

Use the following checklist when validating a real study against Prolific:

1. Create an experiment from `/admin` with the intended name and ratings-per-question.
2. Upload the CSV on the experiment detail page and confirm the question count matches the file.
3. In `Prolific Study Rounds`, complete the pilot form and click `Run Pilot Study`. The default pilot size is `5` raters and is usually a reasonable starting point.
4. Open the draft on Prolific and review the study details before publishing.
5. Publish the draft manually on Prolific when you are ready for participants to start.
6. Wait for the pilot to complete, then review the recommendation panel. If the reported average time per question looks implausible, investigate before launching another round.
7. Launch the next round from the recommendation panel, publish it manually on Prolific, and wait for completion.
8. Repeat until the application reports `All questions have enough ratings!`.

---

## Tailscale (optional)

[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) gives your local machine a
public `https://<machine>.<tailnet>.ts.net` URL. Use it to test a live Prolific study
against your local stack — see real rater traffic in your terminal, iterate without
deploying to Render.

### Prerequisites

- [Tailscale](https://tailscale.com/download) installed and authenticated
- Funnel enabled for your tailnet ([setup guide](https://tailscale.com/kb/1223/funnel))

### Usage

```bash
make tailscale.up       # expose / → frontend, /api → backend
make tailscale.status   # show current Funnel URLs
make tailscale.down     # tear down
```

After `make tailscale.up`, use the `*.ts.net` URL as your study URL in Prolific.
Your local `make up` + `cd frontend && make up` stack serves traffic as-is — no
config changes needed.

---

## Render Deployment

Deploys are GitHub-driven and API-triggered (Render auto-deploy is off):

- `human-rating-platform-web` — static frontend
- `human-rating-platform-api` — FastAPI backend
- `human-rating-platform-db` — Postgres

### Backend runtime on Render

- **Build:** `uv sync --frozen --no-dev --no-install-project`
- **Predeploy:** `sh scripts/predeploy.sh`
- **Start:** `uv run --no-sync uvicorn main:app --host 0.0.0.0 --port $PORT`

### Required GitHub Actions secrets

Set in repo → **Settings** → **Secrets and variables** → **Actions**:

| Secret | Where to find it |
| --- | --- |
| `RENDER_API_KEY` | Render Dashboard → Account Settings → API Keys |
| `RENDER_API_SERVICE_ID` | Render Dashboard → API service → Settings → ID (`srv-...`) |
| `RENDER_WEB_SERVICE_ID` | Render Dashboard → Web service → Settings → ID (`srv-...`) |

### Required Render service env vars

**API service** (set in Render Dashboard → API service → Environment):
- `DATABASE__URL` — Render Postgres internal connection string
- `APP__CORS_ORIGINS` — JSON array including web origin, e.g. `["https://human-rating-platform-web.onrender.com"]`
- `APP__SITE_URL` — public frontend URL, e.g. `https://human-rating-platform-web.onrender.com`
- `PROLIFIC__API_TOKEN` — Prolific API token (set to enable automated study management)
- `PROLIFIC__PROJECT_ID` — Prolific project ID to create studies under (recommended on multi-workspace accounts to avoid wrong-currency studies; workspace and currency are auto-derived)

**Web service** (set in Render Dashboard → Web service → Environment):
- `VITE_API_HOST` — public API origin, e.g. `https://human-rating-platform-api-uxnt.onrender.com`

### Deploy flow

1. CI workflow (`✅ CI Checks`) runs lint + characterization tests.
2. Deploy workflow (`🚀 Deploy Render`) gates on CI success for the same SHA.
3. Target resolution (`api`, `web`, `both`, `none`) is handled by `scripts/resolve_deploy_target.sh`.
4. Deploy execution and polling is handled by `scripts/deploy.sh`.
5. Deploys are commit-pinned — Render builds the exact SHA that passed CI.

### Quick verification

1. Trigger deploy (push to deploy branch, or manual dispatch).
2. Check workflow summary for expected target.
3. Verify web app network requests hit `.../api/...`.
4. Hit health check: `https://<api-host>/api/health`.

---

## CI Checks (local)

Run these locally to match what CI enforces:

```bash
uvx ruff==0.15.2 check backend
uvx ruff==0.15.2 format --check backend
npm --prefix frontend run lint
npm --prefix frontend run typecheck
uvx yamllint==1.38.0 .
```

---

## CSV Format

Upload questions as CSV. See `sample_questions.csv` for a working example.

| Column | Required | Description |
| --- | --- | --- |
| `question_id` | Yes | Unique identifier |
| `question_text` | Yes | Prompt shown to raters |
| `gt_answer` | No | Ground-truth answer |
| `options` | No | Comma-separated options for MC |
| `question_type` | No | `MC` or `FT` (default `MC`) |
| `metadata` | No | JSON string |

```csv
question_id,question_text,gt_answer,options,question_type
q1,"Is the sky blue?","Yes","Yes,No,Maybe",MC
q2,"Explain photosynthesis","Plants convert sunlight...",,FT
```

---

## Assistance Methods

Experiments can be configured with an assistance method to provide raters with AI- or human-generated help while they rate questions. The method is set per-experiment via two fields:

- `assistance_method` — the method name (e.g. `"none"`, `"human_as_tool"`); defaults to `"none"`
- `assistance_params` — JSON-encoded method-specific configuration

### How it works

When a rater opens a question, the frontend calls `POST /api/raters/assistance/start` with the `question_id`. The backend starts an `AssistanceSession` by invoking the configured method's `start()` implementation, which returns an `InteractionStep`. The step has a `type` that tells the frontend what to render:

| `type` | Meaning |
| --- | --- |
| `none` | No assistance available for this question (terminal) |
| `display` | Show static content to the rater (terminal) |
| `ask_input` | Ask the rater a sub-question; call `/assistance/advance` with their answer |
| `complete` | Multi-turn interaction finished; show final result (terminal) |

For multi-turn methods (`ask_input`), the frontend collects the rater's response and calls `POST /api/raters/assistance/advance` with `session_id` and `human_input`. This repeats until a terminal step type is returned.

When the rater submits their rating, they can include the `assistance_session_id` to link the rating to the assistance session for analytics.

### Adding a new method

Create a class in `backend/services/assistance/methods/` that subclasses `AssistanceMethod` and implements `start()` and `advance()`, then register it in the registry. The `none` method in `methods/none.py` is the minimal reference implementation.

---

## Prolific Integration

Prolific integration is enabled automatically when `PROLIFIC__API_TOKEN` is set. When enabled, the platform creates, publishes, and deletes studies on Prolific. When the token is absent, the Prolific round workflow is hidden in the UI.

Typical workflow:

1. **Create an experiment** in the admin UI.
2. **Upload questions** via CSV.
3. In **Prolific Study Rounds**, enter the pilot details and click **Run Pilot Study**.
4. The backend creates a **draft** study on Prolific with the correct study URL and completion code.
5. **Preview** the rater experience using **Preview as Participant**.
6. **Publish** the study from the experiment detail page when ready.
7. **Delete** an experiment and the linked Prolific study is cleaned up automatically.

### Study URL format

Prolific fills in the `{{...}}` placeholders at runtime:

```text
https://your-app.com/rate?experiment_id=1&PROLIFIC_PID={{%PROLIFIC_PID%}}&STUDY_ID={{%STUDY_ID%}}&SESSION_ID={{%SESSION_ID%}}
```

---

## API Endpoints

Interactive Swagger docs are available at `/docs` when the backend is running.

### Admin

- `POST /api/admin/auth/login` — issue HTTP-only admin cookie for allowlisted email
- `POST /api/admin/auth/logout` — clear admin cookie
- `GET /api/admin/platform-status` — check platform capabilities and Prolific mode
- `POST /api/admin/experiments` — create experiment
- `GET /api/admin/experiments` — list experiments
- `POST /api/admin/experiments/{id}/upload` — upload question CSV
- `GET /api/admin/experiments/{id}/uploads` — list uploads for experiment
- `GET /api/admin/experiments/{id}/stats` — experiment statistics
- `GET /api/admin/experiments/{id}/analytics` — rating analytics
- `GET /api/admin/experiments/{id}/export` — export ratings as CSV
- `POST /api/admin/experiments/{id}/prolific/pilot` — create the pilot round draft
- `GET /api/admin/experiments/{id}/prolific/recommend` — calculate the next-round recommendation
- `GET /api/admin/experiments/{id}/prolific/rounds` — list Prolific rounds for the experiment
- `POST /api/admin/experiments/{id}/prolific/rounds` — create a follow-on round draft
- `POST /api/admin/experiments/{id}/prolific/rounds/{round_id}/publish` — publish an unpublished round
- `POST /api/admin/experiments/{id}/prolific/rounds/{round_id}/close` — close an active round
- `DELETE /api/admin/experiments/{id}` — delete experiment (+ Prolific study if linked)

### Rater

- `POST /api/raters/start` — start rating session
- `GET /api/raters/next-question` — get next question
- `POST /api/raters/submit` — submit a rating
- `GET /api/raters/session-status` — check session status
- `POST /api/raters/end-session` — end session
- `POST /api/raters/assistance/start` — start an assistance session for a question; returns an `AssistanceStepResponse` with `session_id`, `type`, and `content`
- `POST /api/raters/assistance/advance` — advance a multi-turn assistance session; body: `{ session_id, human_input }`

Auth and session flow:
- Start requires `experiment_id`, `PROLIFIC_PID`, `STUDY_ID`, and `SESSION_ID`. Preview links should include placeholders for `STUDY_ID`/`SESSION_ID`.
- On `/raters/start`, the backend returns `rater_session_token`. The frontend stores this and sends `X‑Rater‑Session: <token>` for subsequent rater calls.
- Token shape: `v1.<payload>.<sig>` where payload is base64url JSON `{ rid, eid, iat, exp }` and `sig = HMAC‑SHA256(payload, RATER_SESSION_SECRET_KEY or APP_SECRET_KEY)`.
- The backend verifies signature and TTL, then binds the token’s `experiment_id` to the server‑side rater record to prevent cross‑experiment spoofing.

#### Rater session behavior (refresh, re‑entry)

- Prolific params remain in the URL. They are not considered secrets and are visible on the initial redirect from Prolific.
- The frontend persists the rater session in `sessionStorage` to survive accidental refreshes. On reload, the app restores the session from storage, validates it via `/raters/session-status`, and continues without requiring Prolific params again.
- Storage is cleared when the session completes or expires. If a stored token is expired, the app shows a “Session expired” message.
- Re‑entering via the Prolific link while the session is still active resumes the same rater server‑side and issues a fresh token. If the session has ended or expired, `/raters/start` returns 403.
- Multiple browser tabs are not specially synchronized; the backend prevents duplicate ratings for the same question, but running in two tabs may be confusing and is not recommended.
- The timer does not auto‑submit partial answers on expiry. Submissions after expiry receive 403 from the API.

Operational note: Default TTL matches the session duration (3600s = 60 minutes).

---

## Supported Targets

- **Local runtime:** Docker Compose
- **Hosted deployment:** Render, via GitHub Actions + Render API (`scripts/deploy.sh`)

## License

MIT
