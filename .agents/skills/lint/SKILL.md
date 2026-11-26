---
name: lint
description: Run every linter CI gates on (ruff check + format, eslint, tsc, yamllint) with one command. Use before committing or pushing, or whenever asked to lint the repo.
---

Run the full lint gate set from the repo root:

```bash
make lint
```

This mirrors the lint jobs in `.github/workflows/main.yml`: ruff check, ruff format check, eslint, tsc, yamllint — in that order, stopping at the first failure.

## Prerequisites

- `uv` installed (ruff and yamllint run via `uvx` with pinned versions; no venv needed).
- Frontend dependencies installed. If `frontend/node_modules` is missing, run `npm ci --prefix frontend` first.

## On failure

- **ruff format check**: run `make fmt` to auto-format, then re-run `make lint`.
- **ruff check**: fix the reported issues; `uvx ruff==0.15.2 check backend --fix` handles the auto-fixable ones.
- **eslint / tsc**: fix in `frontend/src`; there is no auto-fix wired up, edit by hand.
- **yamllint**: rules live in `.yamllint.yml`.

Re-run `make lint` after fixing until it passes, then report the result.
