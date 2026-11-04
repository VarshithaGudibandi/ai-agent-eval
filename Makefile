.PHONY: up down logs ps migrate db test test-paid lint format typecheck check

up:
	docker compose up -d --build --wait

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

test: db
	uv run alembic upgrade head
	uv run alembic check
	uv run pytest

test-paid: db
	uv run alembic upgrade head
	RUN_PAID_TESTS=1 uv run pytest tests/paid -s

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy app cli

check: lint typecheck test
