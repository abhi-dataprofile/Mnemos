# Mnemos convenience targets.
#
# These are thin wrappers around docker/npm/pip commands so the README can
# say `make up` and `make test` instead of a tutorial.

COMPOSE := docker compose -f docker/docker-compose.yml --env-file .env

.DEFAULT_GOAL := help

.PHONY: help
help:
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} \
		/^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)

# ---------- local stack -----------------------------------------------------

.PHONY: env
env: ## Create .env from .env.example if missing
	@if [ ! -f .env ]; then cp .env.example .env; echo "created .env from .env.example"; else echo ".env already exists"; fi

.PHONY: build
build: ## Build both service images
	$(COMPOSE) build

.PHONY: up
up: ## Start the full stack in the background
	$(COMPOSE) up -d

.PHONY: up-fg
up-fg: ## Start the full stack in the foreground (logs stream)
	$(COMPOSE) up

.PHONY: down
down: ## Stop and remove containers (volumes retained)
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop and remove containers + volumes (destructive)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail logs for all services
	$(COMPOSE) logs -f

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps

# ---------- per-service -----------------------------------------------------

.PHONY: py-test
py-test: ## Run the Python test suite in the host venv
	cd python && python -m pytest -q

.PHONY: py-lint
py-lint: ## Ruff + mypy
	cd python && ruff check . && mypy src

.PHONY: ts-test
ts-test: ## Vitest
	cd typescript && npm test

.PHONY: ts-lint
ts-lint: ## Biome
	cd typescript && npm run lint

.PHONY: ts-typecheck
ts-typecheck: ## tsc --noEmit
	cd typescript && npm run typecheck

# ---------- Phase 1 smoke ---------------------------------------------------

.PHONY: smoke
smoke: ## End-to-end: health endpoints respond, TS can POST to Python
	@echo "waiting for python-api..."
	@until curl -fsS http://localhost:8000/health >/dev/null; do sleep 1; done
	@echo "python-api up"
	@echo "waiting for github-app callback server..."
	@until curl -fsS http://localhost:3001/healthz >/dev/null; do sleep 1; done
	@echo "github-app up"
