SHELL := /bin/sh

.PHONY: help env.sync up down ps logs db.clear db.reset db.new db.up db.down db.seed test config.check fmt tailscale.up tailscale.down tailscale.status

COMPOSE ?= docker compose
TEST_COMPOSE_PROJECT ?= human-rating-platform-test
TEST_COMPOSE_FILE ?= docker-compose.test.yml
COMPOSE_TEST = $(COMPOSE) -f $(TEST_COMPOSE_FILE) -p $(TEST_COMPOSE_PROJECT)
TEST_SERVICE ?= test
KEEP_TEST_STACK ?= 0
MIGRATION_NAME ?= schema_update
MIGRATION_REVISION ?= -1
CONFIG_TARGET ?= local
ENV_SYNC_TARGETS := up down ps logs db.clear db.reset db.new db.up db.down db.seed test config.check

$(ENV_SYNC_TARGETS): env.sync

ifdef NO_COLOR
C_RESET :=
C_BOLD :=
C_DIM :=
C_PRIMARY :=
C_INFO :=
C_OK :=
C_WARN :=
C_ERR :=
C_CMD :=
else
C_RESET := \033[0m
C_BOLD := \033[1m
C_DIM := \033[2m
C_PRIMARY := \033[36m
C_INFO := \033[94m
C_OK := \033[32m
C_WARN := \033[33m
C_ERR := \033[31m
C_CMD := \033[35m
endif

define _title
@printf "\n$(C_BOLD)$(C_PRIMARY)========================================$(C_RESET)\n"
@printf "$(C_BOLD)$(C_PRIMARY)%s$(C_RESET)\n" "$(1)"
@printf "$(C_BOLD)$(C_PRIMARY)========================================$(C_RESET)\n"
endef

define _info
@printf "$(C_INFO)::$(C_RESET) %s\n" "$(1)"
endef

define _ok
@printf "$(C_OK)++$(C_RESET) %s\n" "$(1)"
endef

define _warn
@printf "$(C_WARN)!!$(C_RESET) %s\n" "$(1)"
endef

# Auto-generated help: parses ##@ lines as group headers and ## comments on
# targets as descriptions. Add a new target with `target: ## desc` or a new
# group with `##@` and it appears in `make help` automatically.
help: ## Show available commands
	@printf "$(C_BOLD)$(C_PRIMARY)HUMAN RATING PLATFORM$(C_RESET)\n"
	@printf "$(C_DIM)Modern local workflow commands$(C_RESET)\n"
	@printf "$(C_DIM)Set NO_COLOR=1 to disable styling$(C_RESET)\n\n"
	@awk ' \
		/^##@/ { printf "\n$(C_BOLD)%s$(C_RESET)\n", substr($$0, 5); next } \
		/^[a-zA-Z_.%-]+:.*##/ { \
			target = $$1; sub(/:.*/, "", target); \
			help = $$0; sub(/.*## */, "", help); \
			printf "  $(C_CMD)%-18s$(C_RESET) %s\n", "make " target, help \
		} \
	' $(MAKEFILE_LIST)

##@ Setup
env.sync: ## Create backend/.env, frontend/.env, and frontend/.env.local when missing
	@if [ ! -f backend/.env ]; then \
		cp backend/.env.example backend/.env; \
		printf "$(C_OK)++$(C_RESET) Created backend/.env from backend/.env.example\n"; \
	fi
	@if [ ! -f frontend/.env ]; then \
		cp frontend/.env.example frontend/.env; \
		printf "$(C_OK)++$(C_RESET) Created frontend/.env from frontend/.env.example\n"; \
	fi
	@if [ ! -f frontend/.env.local ]; then \
		cp frontend/.env.local.example frontend/.env.local; \
		printf "$(C_OK)++$(C_RESET) Created frontend/.env.local from frontend/.env.local.example\n"; \
	fi

##@ Core
up: ## Start db + alembic migrations + api (hot reload)
	$(call _title,==> Bringing up local services)
	$(call _info,Step 1/3 - Starting Postgres)
	@$(COMPOSE) up -d db > /dev/null
	$(call _info,Step 2/3 - Applying Alembic migrations)
	@$(COMPOSE) up -d migrate > /dev/null
	$(call _info,Step 3/3 - Starting API (hot reload))
	@$(COMPOSE) up -d api > /dev/null
	$(call _ok,Services are up)
	@$(MAKE) --no-print-directory ps

down: ## Stop services (keep database volume)
	$(call _title,==> Stopping local services)
	@$(COMPOSE) down --remove-orphans > /dev/null
	$(call _ok,Services stopped)

ps: ## Show running compose services
	$(call _title,==> Service status)
	@$(COMPOSE) ps

logs: ## Tail db/api logs
	$(call _title,==> Streaming logs)
	$(call _info,Press Ctrl+C to stop following logs)
	@$(COMPOSE) logs -f --tail=200 db api

##@ Database
db.clear: ## Reset development database (destructive)
	$(call _title,==> Resetting local database)
	$(call _warn,This deletes local Postgres data volumes.)
	@$(COMPOSE) down --volumes --remove-orphans > /dev/null
	$(call _ok,Local database volumes removed)

db.reset: ## Rebuild database from migrations and start API
	$(call _title,==> Rebuilding database and services)
	@$(MAKE) --no-print-directory db.clear
	@$(MAKE) --no-print-directory up
	$(call _ok,Database reset complete)

db.up: ## Apply migrations to head
	$(call _title,==> Applying Alembic migrations)
	@$(COMPOSE) up -d db > /dev/null
	@until $(COMPOSE) exec -T db pg_isready -U postgres -d human_rating_platform > /dev/null 2>&1; do sleep 1; done
	@$(COMPOSE) run --rm --no-deps migrate
	$(call _ok,Migrations applied)

db.down: ## Roll back one migration (set MIGRATION_REVISION=... to override)
	$(call _title,==> Rolling back Alembic migration $(MIGRATION_REVISION))
	@$(COMPOSE) up -d db > /dev/null
	@until $(COMPOSE) exec -T db pg_isready -U postgres -d human_rating_platform > /dev/null 2>&1; do sleep 1; done
	@$(COMPOSE) run --rm --no-deps migrate sh -c "uv sync --frozen --no-dev --no-install-project && sh scripts/migrate.sh downgrade '$(MIGRATION_REVISION)'"
	$(call _ok,Rollback applied)

db.new: ## Create timestamped autogen migration (set MIGRATION_NAME=...)
	$(call _title,==> Creating Alembic migration $(MIGRATION_NAME))
	@$(COMPOSE) up -d db > /dev/null
	@until $(COMPOSE) exec -T db pg_isready -U postgres -d human_rating_platform > /dev/null 2>&1; do sleep 1; done
	@$(COMPOSE) run --rm --no-deps migrate sh -c "uv sync --frozen --no-dev --no-install-project && sh scripts/migrate.sh revision --autogenerate --rev-id \"$$(date -u +%Y%m%d%H%M%S)\" -m '$(MIGRATION_NAME)'"
	$(call _ok,Migration created)

db.seed: ## Seed local dataset from backend/config.toml
	$(call _title,==> Seeding local dataset)
	@$(COMPOSE) up -d db > /dev/null
	@until $(COMPOSE) exec -T db pg_isready -U postgres -d human_rating_platform > /dev/null 2>&1; do sleep 1; done
	@$(COMPOSE) run --rm --no-deps migrate sh -c "\
		uv sync --frozen --no-dev --no-install-project && \
		sh scripts/migrate.sh upgrade head && \
		uv run --no-sync python scripts/seed_dev.py"
	$(call _ok,Seed command finished)

##@ Testing
test: ## Run backend test suite in an isolated compose stack (safe alongside make up)
	$(call _title,==> Running backend tests)
	$(call _info,Using isolated compose file: $(TEST_COMPOSE_FILE))
	$(call _info,Using isolated compose project: $(TEST_COMPOSE_PROJECT))
	$(call _info,Preparing db)
	@set +e; \
	$(COMPOSE_TEST) up -d db > /dev/null; \
	until $(COMPOSE_TEST) exec -T db pg_isready -U postgres -d human_rating_platform > /dev/null 2>&1; do sleep 1; done; \
	printf "$(C_INFO)::$(C_RESET) Applying migrations synchronously\n"; \
	$(COMPOSE_TEST) run --rm --no-deps migrate > /dev/null; \
	printf "$(C_INFO)::$(C_RESET) Executing test service: $(TEST_SERVICE)\n"; \
	$(COMPOSE_TEST) run --rm --no-deps $(TEST_SERVICE); \
	exit_code=$$?; \
	if [ "$(KEEP_TEST_STACK)" != "1" ]; then \
		$(COMPOSE_TEST) down --remove-orphans > /dev/null; \
	else \
		printf "$(C_WARN)!!$(C_RESET) Keeping compose test stack up (KEEP_TEST_STACK=1)\n"; \
	fi; \
	if [ $$exit_code -eq 0 ]; then \
		printf "$(C_OK)++$(C_RESET) Tests passed\n"; \
	else \
		printf "$(C_ERR)xx$(C_RESET) Tests failed (exit=$$exit_code)\n"; \
	fi; \
	exit $$exit_code

##@ Quality
config.check: ## Run backend config validation (optional)
	$(call _title,==> Validating backend config)
	@$(COMPOSE) run --rm --no-deps migrate sh -c "uv sync --frozen --no-dev --no-install-project && uv run --no-sync python scripts/config_check.py --target $(CONFIG_TARGET)"
	$(call _ok,Config check passed)

fmt: ## Format backend Python with ruff
	$(call _title,==> Formatting backend Python)
	@uvx ruff==0.15.2 format backend
	$(call _ok,Formatting complete)

##@ Tailscale
tailscale.up: ## Expose local stack via Tailscale (both frontend and backend)
	$(call _title,==> Starting Tailscale Proxy)
	@tailscale serve reset || true
	@tailscale funnel reset || true
	@tailscale serve --bg --set-path / http://127.0.0.1:5173
	@tailscale funnel --bg --set-path / http://127.0.0.1:5173
	@tailscale serve --bg --set-path /api http://127.0.0.1:8000
	@tailscale funnel --bg --set-path /api http://127.0.0.1:8000
	$(call _ok,Tailscale configured / -> Vite and /api -> Backend)

tailscale.down: ## Remove Tailscale exposure
	$(call _title,==> Stopping Tailscale Proxy)
	@tailscale serve reset || true
	@tailscale funnel reset || true
	$(call _ok,Tailscale reset)

tailscale.status: ## Show Tailscale proxy status
	$(call _title,==> Tailscale Proxy Status)
	@tailscale serve status || true
	@tailscale funnel status || true
