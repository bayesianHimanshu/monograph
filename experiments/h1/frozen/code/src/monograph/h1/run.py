"""Running H1: configurations, and one configuration on one split (docs/h1/PLAN.md, "Models, runs and budget").

* `claude-haiku-5-5`: all five arms, every type.
* `claude-sonnet-5-5`: SV, ONT+OC and W-raw, on Q2 to Q5.

One configuration runs through the shared split runner (`qa.run.run_split`) with H1's answer model, scoring
and per-question arms (each question gets a fresh gate). The response cache tag is `h1/<arm>`. The client is
injected: the cached Anthropic client (behind the spend guard on test runs) from the CLI, a fake in tests.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from monograph.agent.llm import ToolClient
from monograph.h1 import arms, pins, record
from monograph.h1.score import H1Answer
from monograph.qa.run import Writer, run_split
from monograph.sqlcheck.check import OntologyChecker
from monograph.tools.freeform import QueryIn, QueryOut, SqlRunner

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
MODELS: tuple[str, ...] = (HAIKU, SONNET)
SONNET_ARMS: tuple[str, ...] = ("SV", "ONT+OC", "W-raw")
SONNET_TYPES: tuple[str, ...] = ("Q2", "Q3", "Q4", "Q5")
METRIC = "h1"


@dataclass(frozen=True, slots=True)
class Config:
    model: str
    arm: str

    @property
    def tag(self) -> str:
        return f"h1/{self.arm}"


def configs() -> list[Config]:
    return [Config(HAIKU, a) for a in arms.ARMS] + [Config(SONNET, a) for a in SONNET_ARMS]


def types_for(model: str) -> tuple[str, ...] | None:
    """The question types a model runs (None: every type)."""
    return SONNET_TYPES if model == SONNET else None


def prompt_versions(arm: str) -> dict[str, str]:
    v = arms.PROMPT_VERSIONS
    return {**v, arm: v[arms.CONTEXT[arm]]}


def connection_executor(dsn: str) -> Callable[[], Callable[[str], QueryOut]]:
    """One read-only connection per worker thread, reused across that worker's questions."""
    local = threading.local()

    def factory() -> Callable[[str], QueryOut]:
        if not hasattr(local, "runner"):
            local.runner = SqlRunner(psycopg.connect(dsn, autocommit=True), "")
        runner: SqlRunner = local.runner
        return lambda query: runner.run_sql(QueryIn(query=query))

    return factory


def run_config(
    root: Path,
    questions: Sequence[Mapping[str, Any]],
    config: Config,
    split: str,
    client: ToolClient,
    write: Writer,
    executor: Callable[[], Callable[[str], QueryOut]],
    workers: int = 4,
    log: Callable[[str], None] = print,
    extra: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    ctx = arms.CONTEXT[config.arm]
    text = pins.describe_text(root, ctx)
    checker = OntologyChecker(pins.catalog(root), arms.SEARCH_PATH[arms.DATABASE[ctx]])
    tools = pins.tools(root)

    def make() -> arms.H1Arm:
        return arms.make_arm(config.arm, text, executor(), checker, tools)

    return run_split(
        questions,
        config.arm,
        config.model,
        split,
        make,
        pins.system_prompt(root, config.arm),
        client,
        write,
        None,
        workers,
        settings=arms.SETTINGS,
        log=log,
        metric=METRIC,
        tag=config.tag,
        extra={"context": ctx, "checked": arms.checked(config.arm), **(extra or {})},
        prompt_versions=prompt_versions(config.arm),
        answer_model=H1Answer,
        score=record.score,
        fresh_arm=True,
        annotate=record.annotate,
    )
