.PHONY: up down logs ps migrate db test lint format typecheck check

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
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy app

check: lint typecheck test
