"""The five H1 arms: context (semantic layer or ontology) x check (off or on), plus the W-raw floor.

Every arm gets the same base prompt, model, settings (at most 12 data tool calls), answer schema, tools
(`describe_data` and `run_sql`) and execution-error retry. They differ only in what `describe_data` returns,
which database (and so which schemas) `run_sql` reads, a one-line addendum naming that context, and, for the
OC arms, a sentence saying queries are checked and whether the check blocks (enforce) or only records
(shadow).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from monograph.agent.arms import Arm, load_prompt
from monograph.agent.llm import AgentSettings
from monograph.h1.score import SUBMIT
from monograph.sqlcheck.check import OntologyChecker
from monograph.sqlcheck.gate import QueryGate
from monograph.tools.freeform import NoInput, QueryIn, QueryOut
from monograph.tools.registry import ToolError

ARMS: tuple[str, ...] = ("W-raw", "SV", "SV+OC", "ONT", "ONT+OC")
Context = Literal["W-raw", "SV", "ONT"]
CONTEXT: dict[str, Context] = {"W-raw": "W-raw", "SV": "SV", "SV+OC": "SV", "ONT": "ONT", "ONT+OC": "ONT"}
DATABASE: dict[Context, Literal["wh", "sv"]] = {"W-raw": "wh", "SV": "sv", "ONT": "wh"}
SEARCH_PATH: dict[Literal["wh", "sv"], tuple[str, ...]] = {"wh": ("wh",), "sv": ("wh", "sem")}
PROMPT_VERSIONS: dict[str, str] = {"base": "v1", "W-raw": "v1", "SV": "v1", "ONT": "v1", "check": "v1"}

SETTINGS = AgentSettings(
    max_tokens=16000,
    effort="medium",
    max_tool_calls=12,
    max_turns=16,
    max_output_tokens=64000,
    result_char_cap=60000,
)

DESCRIBE_DESCRIPTION = "Describe the data you can query: what it holds and how it is organised."
SQL_DESCRIPTION = (
    "Run one read-only SQL query (PostgreSQL 18) and get up to 200 rows. A single SELECT or WITH statement; the"
    " statement timeout is 10 s."
)


def checked(arm: str) -> bool:
    return arm.endswith("+OC")


def system_prompt(arm: str, versions: dict[str, str] = PROMPT_VERSIONS) -> str:
    ctx = CONTEXT[arm]
    parts = [load_prompt("h1_base", versions["base"]), load_prompt(f"h1_arm_{ctx}", versions[ctx])]
    if checked(arm):
        parts.append(load_prompt("h1_check", versions["check"]))
    return "\n\n".join(p.rstrip() for p in parts)


def _schema(model: type[NoInput] | type[QueryIn]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema["additionalProperties"] = False
    return schema


TOOLS: tuple[dict[str, Any], ...] = (
    {"name": "describe_data", "description": DESCRIBE_DESCRIPTION, "input_schema": _schema(NoInput)},
    {"name": "run_sql", "description": SQL_DESCRIPTION, "input_schema": _schema(QueryIn)},
    SUBMIT,
)


@dataclass(frozen=True)
class H1Arm(Arm):
    gate: QueryGate | None = field(default=None, compare=False)


def make_arm(
    name: str,
    describe_text: str,
    execute: Callable[[str], QueryOut],
    checker: OntologyChecker,
    tools: tuple[dict[str, Any], ...] = TOOLS,
) -> H1Arm:
    """One arm for one question: a fresh gate (repair rounds and retries are per question)."""
    gate = QueryGate(checker, execute, "enforce" if checked(name) else "shadow")

    def run(tool: str, arguments: dict[str, Any]) -> str:
        try:
            if tool == "describe_data":
                NoInput.model_validate(arguments)
                return describe_text
            if tool == "run_sql":
                return gate.run_sql(QueryIn.model_validate(arguments).query)
        except ValidationError as exc:
            raise ToolError(f"invalid arguments: {exc.errors(include_url=False)}") from exc
        raise ToolError(f"unknown tool {tool!r}")

    return H1Arm(name, "sql", "free", tools, run, gate)
