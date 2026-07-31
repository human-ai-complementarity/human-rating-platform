SHELL := /bin/sh
PORT ?= 5173

.PHONY: help deps up up.bg stop

help:
	@printf "Frontend commands\n"
	@printf "  make up      Install deps when needed, then run Vite dev server on 0.0.0.0:$(PORT) (foreground)\n"
	@printf "  make up.bg   Same, but detached — logs to frontend/vite.log (used by root 'make dev.up')\n"
	@printf "  make stop    Stop a detached dev server (whatever is listening on :$(PORT))\n"
	@printf "  make deps    Install deps only when package.json/package-lock.json changed\n"

deps:
	@set -e; \
	current=$$(shasum package-lock.json package.json | shasum | awk '{print $$1}'); \
	stored=$$(cat node_modules/.deps-hash 2>/dev/null || true); \
	if [ ! -d node_modules ] || [ "$$current" != "$$stored" ]; then \
		echo "Installing frontend dependencies..."; \
		npm ci; \
		mkdir -p node_modules; \
		echo "$$current" > node_modules/.deps-hash; \
	else \
		echo "Frontend dependencies already up to date."; \
	fi

# --strictPort so the server fails loudly if $(PORT) is taken rather than
# drifting to $(PORT)+1 — that keeps `stop` (which kills by port) reliable.
up: deps
	npm run dev -- --host 0.0.0.0 --port $(PORT) --strictPort

up.bg: deps
	@if lsof -ti:$(PORT) >/dev/null 2>&1; then \
		echo "Frontend already running on :$(PORT)."; \
	else \
		if command -v setsid >/dev/null 2>&1; then \
			setsid npm run dev -- --host 0.0.0.0 --port $(PORT) --strictPort </dev/null > vite.log 2>&1 & \
		else \
			( nohup npm run dev -- --host 0.0.0.0 --port $(PORT) --strictPort </dev/null > vite.log 2>&1 & ) ; \
		fi ; \
		echo "Frontend starting on http://localhost:$(PORT) (logs: frontend/vite.log)"; \
	fi

stop:
	@pids=$$(lsof -ti:$(PORT) 2>/dev/null); \
	if [ -n "$$pids" ]; then kill $$pids && echo "Frontend stopped (:$(PORT))."; \
	else echo "Frontend not running (:$(PORT))."; fi
