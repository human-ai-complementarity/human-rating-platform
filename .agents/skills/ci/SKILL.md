---
name: ci
description: Run the full CI gate set locally (lint + backend tests + Playwright e2e) exactly as GitHub Actions runs it. Use before opening or updating a PR, or before declaring a change done.
---

Run the full CI gate set from the repo root:

```bash
make ci
```

This runs, in order, stopping at the first failure:

1. `make lint` — ruff check, ruff format check, eslint, tsc, yamllint.
2. `make test` — backend suite in an isolated Docker compose stack (`docker-compose.test.yml`, project `human-rating-platform-test`; safe to run alongside `make up`).
3. `npm --prefix frontend run test:e2e` — Playwright e2e suite.

It mirrors `.github/workflows/main.yml`; if `make ci` passes locally, the PR checks should pass.

## Prerequisites

- Docker running (backend tests).
- `uv` installed (linters run via pinned `uvx`).
- Frontend dependencies installed: `npm ci --prefix frontend` if `frontend/node_modules` is missing.
- Playwright chromium installed: `cd frontend && npx playwright install chromium` (one-time). On Linux use `npx playwright install --with-deps chromium` (requires sudo) so the OS libraries headless Chromium needs are installed too — this matches what CI runs.

## Notes

- The full set takes several minutes; give the Bash call a long timeout (10 minutes).
- Never run backend tests via pytest against a local venv — always `make test`.
- On a lint failure, see the `lint` skill for per-gate fixes, then re-run `make ci`.
- On a backend test failure, re-run with `KEEP_TEST_STACK=1 make test` to keep the compose stack up for inspection (`docker compose -f docker-compose.test.yml -p human-rating-platform-test logs`).
- On a Playwright failure, traces land in `frontend/test-results/`.

Re-run until everything passes, then report the result.
