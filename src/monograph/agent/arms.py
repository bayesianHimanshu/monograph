"""The four arms: store (relational or graph) x interface (free-form queries or parameterised tools).

Every arm gets the same base system prompt, the same final answer tool and the same budgets. An arm's
own addendum names its tools; the tool definitions (names, input schemas, descriptions) come from the
shared registry for the tools arms, and from `tools/freeform.py` for the free-form arms.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from monograph.tools.freeform import (
    CYPHER_DESCRIPTION,
    SCHEMA_DESCRIPTION,
    SQL_DESCRIPTION,
    CypherRunner,
    NoInput,
    QueryIn,
    SqlRunner,
)
from monograph.tools.registry import RESOLVE_EXACT_DESCRIPTION, TOOLS, ToolError, ToolSpec, tool_schema
from monograph.tools.service import ToolService

ARMS: tuple[str, ...] = ("S-free", "S-tools", "G-free", "G-tools")
PROMPT_VERSIONS: dict[str, str] = {"base": "v1", "S-free": "v1", "S-tools": "v1", "G-free": "v1", "G-tools": "v1"}


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["ids", "number", "none"]
    ids: list[str] = Field(default=[], description="kind ids: the complete set of canonical ids.")
    number: int | None = Field(default=None, description="kind number: the count.")
    evidence: list[int] = Field(default=[], description="Numbers of the tool calls that produced the answer.")


SUBMIT = {
    "name": "submit_answer",
    "description": (
        "Submit the final answer, once. kind 'ids' with the complete set of canonical ids, 'number' with the"
        " count, or 'none' when the entity does not exist or nothing satisfies the question; evidence lists the"
        " numbers of the tool calls that produced it."
    ),
    "input_schema": {**FinalAnswer.model_json_schema(), "additionalProperties": False},
}


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema["additionalProperties"] = False
    return schema


def load_prompt(name: str, version: str) -> str:
    return (resources.files("monograph.agent") / "prompts" / f"{name}_{version}.md").read_text(encoding="utf-8")


def system_prompt(arm: str, versions: dict[str, str] = PROMPT_VERSIONS) -> str:
    return load_prompt("base", versions["base"]).rstrip() + "\n\n" + load_prompt(f"arm_{arm}", versions[arm]).rstrip()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


Executor = Callable[[str, dict[str, Any]], str]


@dataclass(frozen=True)
class Arm:
    name: str
    store: Literal["sql", "graph"]
    interface: Literal["free", "tools"]
    tools: tuple[dict[str, Any], ...]  # API tool definitions, submit_answer last
    execute: Executor  # raises ToolError with a message for the model

    def tools_hash(self) -> str:
        return sha256(json.dumps(list(self.tools), sort_keys=True))


def _tool_description(t: ToolSpec, identity: bool) -> str:
    return RESOLVE_EXACT_DESCRIPTION if t.name == "resolve_entity" and not identity else t.description


def tools_arm(name: str, service: ToolService) -> Arm:
    """The registry's tools over one store. With identity off (H2.1), resolve_entity's description states its
    exact matching; every other definition is the same in every cell."""
    defs = tuple(
        {"name": t.name, "description": _tool_description(t, service.identity), "input_schema": tool_schema(t)}
        for t in TOOLS
    )

    def execute(tool: str, arguments: dict[str, Any]) -> str:
        try:
            return service.call(tool, arguments).model_dump_json()
        except ValidationError as exc:
            raise ToolError(f"invalid arguments: {exc.errors(include_url=False)}") from exc

    store: Literal["sql", "graph"] = "sql" if name.startswith("S") else "graph"
    return Arm(name, store, "tools", (*defs, SUBMIT), execute)


def free_arm(name: str, runner: SqlRunner | CypherRunner, schema_description: str = SCHEMA_DESCRIPTION) -> Arm:
    if isinstance(runner, SqlRunner):
        query_tool, description, run = "run_sql", SQL_DESCRIPTION, runner.run_sql
    else:
        query_tool, description, run = "run_cypher", CYPHER_DESCRIPTION, runner.run_cypher
    defs = (
        {"name": "describe_schema", "description": schema_description, "input_schema": _schema(NoInput)},
        {"name": query_tool, "description": description, "input_schema": _schema(QueryIn)},
    )

    def execute(tool: str, arguments: dict[str, Any]) -> str:
        try:
            if tool == "describe_schema":
                return runner.describe_schema(NoInput.model_validate(arguments))
            if tool == query_tool:
                return run(QueryIn.model_validate(arguments)).model_dump_json()
        except ValidationError as exc:
            raise ToolError(f"invalid arguments: {exc.errors(include_url=False)}") from exc
        raise ToolError(f"unknown tool {tool!r}")

    store: Literal["sql", "graph"] = "sql" if isinstance(runner, SqlRunner) else "graph"
    return Arm(name, store, "free", (*defs, SUBMIT), execute)
