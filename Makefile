.PHONY: up up-hosted demo down logs ps migrate db test-db test test-paid lint format typecheck check

up:
	docker compose up -d --build --wait

# On a server: HTTPS for DOMAIN through Caddy, nothing else reachable from outside.
up-hosted:
	docker compose -f compose.yaml -f compose.hosted.yaml up -d --build --wait

# Loads the demo workspace into the running stack. Set DEMO_MODE=true to show its sign-in button.
demo:
	docker compose exec api python -m app.demo.seed

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

migrate:
	docker compose run --rm migrate

db:
	docker compose up -d --wait postgres

# Tests use a database of their own, so a running stack's worker never takes their jobs.
TEST_DATABASE_URL ?= postgresql+asyncpg://agent_eval:agent_eval@localhost:5432/agent_eval_test

test-db: db
	echo "SELECT 'CREATE DATABASE agent_eval_test' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'agent_eval_test')\gexec" | docker compose exec -T postgres psql -q -U agent_eval -d agent_eval
	DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic upgrade head

test: test-db
	DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic check
	DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

test-paid: test-db
	DATABASE_URL=$(TEST_DATABASE_URL) RUN_PAID_TESTS=1 uv run pytest tests/paid -s

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy app sdk/agent_eval cli/evalctl mcp/agent_eval_mcp

check: lint typecheck test
