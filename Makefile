PG_DSN ?= postgresql://monograph:monograph@localhost:55432/monograph
export MONOGRAPH_PG_DSN ?= $(PG_DSN)

.PHONY: gen check test lint it mutate report measure up down

gen:
	uv run monograph gen

check:
	uv run monograph check all

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy

test:
	uv run pytest -q

up:
	docker compose up -d --wait postgres

down:
	docker compose down

it: up
	uv run pytest -q -m integration

measure:
	uv run monograph measure

mutate:
	uv run monograph mutate

report: measure
	uv run monograph report h6
