# Human Rating Platform

FastAPI backend + React frontend for running human rating experiments on Prolific. Setup, architecture, and workflows are in [README.md](README.md); `make help` lists all targets.

## CI checks

Before pushing or opening/updating a PR, run the full CI gate set locally and make it pass:

```bash
make ci     # lint + backend tests + Playwright e2e; mirrors .github/workflows/main.yml
```

For a fast check while iterating, `make lint` runs every linter CI gates on (ruff check + format, eslint, tsc, yamllint). `make fmt` auto-formats backend Python.

- `make ci` needs Docker running and Playwright chromium installed (`cd frontend && npx playwright install chromium`, one-time; on Linux use `--with-deps` to get the OS libraries too).
- Run backend tests only via `make test` (isolated Docker compose stack), never pytest against a local venv.
- The `lint` and `ci` skills in `.claude/skills/` cover prerequisites and how to fix each gate's failures.
