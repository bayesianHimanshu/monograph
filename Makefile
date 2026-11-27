PG_DSN ?= postgresql://monograph:monograph@localhost:55432/monograph
AGE_DSN ?= postgresql://monograph:monograph@localhost:55433/monograph
export MONOGRAPH_PG_DSN ?= $(PG_DSN)

.PHONY: gen check test lint it mutate report measure up down age age-it hooks

SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -ec

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

age:
	docker compose up -d --wait age

age-it: age
	MONOGRAPH_AGE_DSN=$(AGE_DSN) uv run pytest -q -m age

hooks:
	install -m 0755 tools/pre-commit .git/hooks/pre-commit
