"""Running H2.1: settings, prompts, configurations, arms per cell, and the identity-off failure category.

Every arm, model and cell gets the same settings (`SETTINGS`), base prompt and final answer tool. The arm
addenda for the free-form arms (v2) no longer promise "what it means", which is false with descriptions
off; the tools addenda are H2's v1 unchanged. The response cache tag is `h2_1/<cell>/<arm>`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from monograph.ablation.cells import BASELINE, CELLS, HARDEST, Cell
from monograph.agent.arms import ARMS, load_prompt
from monograph.agent.llm import AgentSettings

MODELS: tuple[str, ...] = ("claude-haiku-5-5", "claude-sonnet-5-5")
HAIKU, SONNET = MODELS

SETTINGS = AgentSettings(
    max_tokens=16000,
    effort="medium",
    max_tool_calls=20,
    max_turns=24,
    max_output_tokens=64000,
    result_char_cap=60000,
)
PROMPT_VERSIONS: dict[str, str] = {"base": "v2", "S-free": "v2", "S-tools": "v1", "G-free": "v2", "G-tools": "v1"}

# Free-form describe_schema: one wording that is true in every cell (H2's promised "what they mean").
SCHEMA_DESCRIPTION = "Describe the data: every table, or every vertex and edge label, with its columns or properties."


def system_prompt(arm: str) -> str:
    v = PROMPT_VERSIONS
    return load_prompt("base", v["base"]).rstrip() + "\n\n" + load_prompt(f"arm_{arm}", v[arm]).rstrip()


@dataclass(frozen=True, slots=True)
class Config:
    model: str
    cell: Cell
    arm: str

    @property
    def tag(self) -> str:
        return f"h2_1/{self.cell.name}/{self.arm}"


# Sonnet's test runs cover these types only (decided by the user on 2026-11-28, after the dev cost estimate of
# USD 52.34 exceeded the USD 40 stop and before the PREREG). Dev ran every type for every configuration; T1, T4
# and T6 were at the ceiling there. P3 and the refined verdict (T9+T10) stay fully covered for both models.
SONNET_TEST_TYPES: tuple[str, ...] = ("T3", "T9", "T10", "T11")


def test_types(model: str) -> tuple[str, ...] | None:
    """The question types a model's test runs cover (None: every type)."""
    return SONNET_TEST_TYPES if model == SONNET else None


def configs() -> list[Config]:
    """Haiku: the full factorial. Sonnet: S-free, G-free and G-tools in the baseline and the hardest cell (G-free
    added on 2026-11-28, before any run, so P3 and the refined verdict can be checked for both models)."""
    out = [Config(HAIKU, cell, arm) for cell in CELLS for arm in ARMS]
    out += [Config(SONNET, cell, arm) for cell in (BASELINE, HARDEST) for arm in ("S-free", "G-free", "G-tools")]
    return out


# ------------------------------------------------------------------------------ crosswalk failures

CROSSWALK = "used a crosswalk that does not exist"

# Tables, columns, edge labels and properties of the identity layer, absent with identity off (generated
# artifacts; tests/test_h2_1_run.py checks these against the cell DDLs and projections).
IDENTITY_SQL = (
    "product__drug_mappings",
    "organization__aliases",
    "substance__active_moiety",
    "clinical_drug__generic",
    "adverse_event_report__drugs__harmonized_clinical_drugs",
    "clinical_drug_iri",
    "ndc11",
)
IDENTITY_EDGES = (
    "CLINICAL_DRUG",
    "DRUG_MAPPING",
    "HARMONIZED_CLINICAL_DRUG",
    "HAS_ACTIVE_MOIETY",
    "HAS_GENERIC",
    "ALIAS",
)
IDENTITY_GRAPH = (*IDENTITY_EDGES, "OrganizationAlias", "ndc11")

_SQL = re.compile(r"\b(" + "|".join(IDENTITY_SQL) + r")\b", re.IGNORECASE)
_CYPHER = re.compile(r"(?<![A-Za-z_])(" + "|".join(IDENTITY_GRAPH) + r")(?![A-Za-z_])")


def crosswalk_calls(calls: Sequence[Mapping[str, Any]]) -> list[int]:
    """Indices of the calls that try to use an identity-layer table, column, edge or option."""
    out = []
    for c in calls:
        name, args = str(c.get("name")), c.get("input") or {}
        hit = False
        if name == "run_sql":
            hit = bool(_SQL.search(str(args.get("query", ""))))
        elif name == "run_cypher":
            hit = bool(_CYPHER.search(str(args.get("query", ""))))
        elif name == "neighbors":
            hit = args.get("relation") in IDENTITY_EDGES
        elif name in ("products_with_substance", "count"):
            hit = bool(args.get("include_moiety_matches"))
        if hit:
            out.append(int(c.get("index", 0)))
    return out


def crosswalk_classifier(cell: Cell) -> Callable[[dict[str, Any]], str | None]:
    """With identity off, a wrong answer whose calls tried an identity-layer element the cell lacks is
    "used a crosswalk that does not exist", ahead of every category but refusal and budget exhausted."""

    def classify(record: dict[str, Any]) -> str | None:
        if cell.identity or record.get("failure") in ("refusal", "budget exhausted"):
            return None
        return CROSSWALK if crosswalk_calls(record.get("calls", [])) else None

    return classify
