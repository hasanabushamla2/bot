.PHONY: help install dev-install test lint format type-check clean run-dashboard run-paper init-db migrate

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	pip install -e .

dev-install: ## Install with dev dependencies
	pip install -e ".[dev]"

test: ## Run all tests
	pytest tests/ -v

test-unit: ## Run unit tests only
	pytest tests/ -v -m "unit or not integration"

test-integration: ## Run integration tests
	pytest tests/ -v -m "integration"

test-backtest: ## Run backtesting tests
	pytest tests/test_backtesting/ -v

lint: ## Run ruff linter
	ruff check src/ tests/

format: ## Format code with ruff
	ruff format src/ tests/

type-check: ## Run mypy type checking
	mypy src/

check: lint type-check test ## Run all checks (lint + type + test)

clean: ## Clean build artifacts
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ htmlcov/ .coverage coverage.xml

init-db: ## Initialize database tables
	python scripts/init_db.py

migrate: ## Run alembic migrations
	alembic upgrade head

run-dashboard: ## Start monitoring dashboard
	uvicorn src.dashboard.app:app --host $(DASHBOARD_HOST) --port $(DASHBOARD_PORT) --reload

run-paper: ## Start paper trading engine
	python -m src.paper.engine

run-all: ## Run full system (paper mode)
	docker compose up -d

stop-all: ## Stop all services
	docker compose down

docker-build: ## Build Docker image
	docker build -t quant-engine:latest .

shell: ## Open Python REPL with src in path
	PYTHONPATH=src python
