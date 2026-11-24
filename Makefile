.PHONY: help install dev lint format typecheck test test-cov test-fast clean build publish pre-commit doctor sandbox-image postgres-up postgres-down audit

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Setup ─────────────────────────────────────────────────────────────────────

install: ## Install project dependencies
	uv sync

dev: ## Install dev dependencies + pre-commit hooks
	uv sync --group dev
	uv run pre-commit install

# ── Code Quality ──────────────────────────────────────────────────────────────

lint: ## Run ruff linter
	uv run ruff check olano/ tests/

format: ## Format code with ruff
	uv run ruff format olano/ tests/
	uv run ruff check --fix olano/ tests/

format-check: ## Check formatting without modifying files
	uv run ruff format --check olano/ tests/

typecheck: ## Run mypy type checker
	uv run mypy olano/ --ignore-missing-imports

check: lint format-check typecheck ## Run all code quality checks (no modifications)

fix: ## Auto-fix all lint and format issues
	uv run ruff check --fix olano/ tests/
	uv run ruff format olano/ tests/

# ── Testing ───────────────────────────────────────────────────────────────────

test: ## Run all tests
	uv run pytest tests/ -q

audit: ## Audit locked dependencies for known vulnerabilities (mirrors the CI gate)
	uv export --frozen --no-emit-project --no-hashes -o /tmp/olano-audit-reqs.txt
	uvx pip-audit -r /tmp/olano-audit-reqs.txt --no-deps --disable-pip --ignore-vuln PYSEC-2026-311 --ignore-vuln GHSA-xf7x-x43h-rpqh --ignore-vuln CVE-2026-45830 --ignore-vuln CVE-2026-45831 --ignore-vuln CVE-2026-45833 --ignore-vuln CVE-2026-49265 --ignore-vuln CVE-2026-49264 --ignore-vuln CVE-2026-49272

test-cov: ## Run tests with coverage report
	uv run pytest tests/ -q --cov=olano --cov-report=term-missing --cov-report=html

test-fast: ## Run tests in parallel (requires pytest-xdist)
	uv run pytest tests/ -q -n auto

test-watch: ## Run tests on file change (requires pytest-watch)
	uv run ptw -- tests/ -q

test-e2e: ## Run browser E2E dashboard tests (needs Node + Playwright Chromium)
	cd olano/dashboard_2.0 && npm install
	uv run playwright install chromium
	uv run pytest tests/e2e -q -m e2e

test-sandbox: ## Run live sandbox tests against a real container (needs Docker + the sandbox image)
	docker pull ghcr.io/olano-ai/olano-sandbox:latest
	docker tag ghcr.io/olano-ai/olano-sandbox:latest olano-sandbox:latest
	uv run pytest tests/integration -q

# ── Build & Release ──────────────────────────────────────────────────────────

build: check test ## Build package (after checks and tests pass)
	uv build

publish: build ## Publish to PyPI (requires PYPI_TOKEN)
	uv publish

# ── Pre-commit ────────────────────────────────────────────────────────────────

pre-commit: ## Run pre-commit on all files
	uv run pre-commit run --all-files

pre-commit-update: ## Update pre-commit hook versions
	uv run pre-commit autoupdate

# ── Utilities ─────────────────────────────────────────────────────────────────

clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ *.egg-info/
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/
	rm -rf htmlcov/ coverage.xml .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

doctor: ## Run olano doctor to check system health
	uv run olano doctor

postgres-up: ## Start local Postgres for the production checkpoint/store backend
	docker compose --profile postgres up -d postgres
	@echo "Postgres up. Point olano at it with:"
	@echo "  export LIONCLAW_CHECKPOINT_BACKEND=postgres"
	@echo "  export LIONCLAW_CHECKPOINT_DB_URL=postgresql://$${OLANO_PG_NAME:-lionclaw}:$${LIONCLAW_PG_PASSWORD:-lionclaw}@localhost:5432/$${OLANO_PG_NAME:-lionclaw}"

postgres-down: ## Stop and remove the local Postgres container
	docker compose --profile postgres down

sandbox-image: ## Build the olano-sandbox Docker image used by OlanoDockerBackend
	docker build -t olano-sandbox:latest docker/olano-sandbox/
