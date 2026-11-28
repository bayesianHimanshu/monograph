"""H2.1 runs: configurations, settings and prompts are the same for every arm and cell; the identity-off failure
category ("used a crosswalk that does not exist") recognizes exactly the identity layer's names."""

from __future__ import annotations

import re

import pytest

from monograph.ablation.cells import BASELINE, CELLS, HARDEST
from monograph.ablation.run import (
    CROSSWALK,
    HAIKU,
    IDENTITY_EDGES,
    IDENTITY_GRAPH,
    IDENTITY_SQL,
    SETTINGS,
    SONNET,
    Config,
    configs,
    crosswalk_calls,
    crosswalk_classifier,
    system_prompt,
)
from monograph.agent.arms import ARMS
from monograph.gen.artifacts import AGE_FILE, DDL_FILE

from .conftest import ROOT


def test_the_pre_registered_configurations() -> None:
    cs = configs()
    assert len(cs) == 22
    assert len({c.tag + c.model for c in cs}) == 22
    haiku = [c for c in cs if c.model == HAIKU]
    assert {(c.cell, c.arm) for c in haiku} == {(cell, arm) for cell in CELLS for arm in ARMS}
    sonnet = [c for c in cs if c.model == SONNET]
    assert {c.cell for c in sonnet} == {BASELINE, HARDEST}
    assert {c.arm for c in sonnet} == {"S-free", "G-free", "G-tools"}


def test_sonnet_tests_cover_the_informative_types_only() -> None:
    from monograph.ablation.run import SONNET_TEST_TYPES, test_types

    assert test_types(HAIKU) is None
    assert test_types(SONNET) == SONNET_TEST_TYPES == ("T3", "T9", "T10", "T11")


def test_the_cache_tag_separates_cells() -> None:
    a, b = Config(HAIKU, CELLS[0], "S-tools"), Config(HAIKU, CELLS[1], "S-tools")
    assert a.tag != b.tag
    assert a.tag == "h2_1/id-on_desc-on/S-tools"


def test_settings_follow_the_plan() -> None:
    assert SETTINGS.max_tool_calls == 20
    assert SETTINGS.max_turns == 24
    assert SETTINGS.max_tokens == 16000
    assert SETTINGS.max_output_tokens == 64000
    assert SETTINGS.result_char_cap == 60000
    assert SETTINGS.refusal_fallbacks is False


def test_prompts_share_one_base_and_state_the_budget_and_steps() -> None:
    prompts = {arm: system_prompt(arm) for arm in ARMS}
    bases = {p.split("\n\nYour tools:")[0] for p in prompts.values()}
    assert len(bases) == 1
    (base,) = bases
    assert "at most 20 tool calls" in base
    assert "A step goes from an entity to a directly related one" in base
    assert prompts["S-tools"].endswith(prompts["G-tools"].split("\n\n")[-1])
    for arm in ("S-free", "G-free"):
        assert "what it means" not in prompts[arm]


def _artifact(cell_identity: bool, file: str) -> str:
    cell = next(c for c in CELLS if c.identity is cell_identity and c.descriptions)
    return (ROOT / "generated" / cell.artifact(file)).read_text(encoding="utf-8")


def test_the_identity_names_are_exactly_what_identity_off_lacks() -> None:
    on_ddl, off_ddl = _artifact(True, DDL_FILE), _artifact(False, DDL_FILE)
    for name in IDENTITY_SQL:
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        assert pattern.search(on_ddl), name
        assert not pattern.search(off_ddl), name
    on_age, off_age = _artifact(True, AGE_FILE), _artifact(False, AGE_FILE)
    for name in IDENTITY_GRAPH:
        assert f'"{name}"' in on_age, name
        assert f'"{name}"' not in off_age, name


@pytest.mark.parametrize(
    ("call", "flagged"),
    [
        ({"name": "run_sql", "input": {"query": "SELECT * FROM product__drug_mappings"}}, True),
        ({"name": "run_sql", "input": {"query": "select owner_iri from ORGANIZATION__ALIASES"}}, True),
        ({"name": "run_sql", "input": {"query": "SELECT iri FROM packaged_product WHERE ndc11 = '1'"}}, True),
        ({"name": "run_sql", "input": {"query": "SELECT iri, name FROM clinical_drug WHERE rxcui = '1'"}}, False),
        ({"name": "run_sql", "input": {"query": "SELECT * FROM product WHERE name ILIKE '%alias%'"}}, False),
        ({"name": "run_cypher", "input": {"query": "MATCH (p:Product)-[:DRUG_MAPPING]->(c) RETURN c"}}, True),
        ({"name": "run_cypher", "input": {"query": "MATCH (s)-[:HAS_ACTIVE_MOIETY]->(m) RETURN m"}}, True),
        ({"name": "run_cypher", "input": {"query": "MATCH (c:ClinicalDrug) RETURN c.iri"}}, False),
        ({"name": "run_cypher", "input": {"query": "MATCH (p:Product)-[:INGREDIENT]->(s) RETURN s"}}, False),
        ({"name": "neighbors", "input": {"ids": ["x"], "relation": "HAS_GENERIC"}}, True),
        ({"name": "neighbors", "input": {"ids": ["x"], "relation": "LABELER"}}, False),
        ({"name": "products_with_substance", "input": {"substance_id": "x", "include_moiety_matches": True}}, True),
        ({"name": "products_with_substance", "input": {"substance_id": "x"}}, False),
        ({"name": "resolve_entity", "input": {"text": "00069100001"}}, False),
    ],
)
def test_crosswalk_calls(call: dict[str, object], flagged: bool) -> None:
    assert crosswalk_calls([{**call, "index": 3}]) == ([3] if flagged else [])


def test_every_identity_edge_is_recognized() -> None:
    for edge in IDENTITY_EDGES:
        assert crosswalk_calls(
            [{"name": "run_cypher", "index": 1, "input": {"query": f"MATCH ()-[:{edge}]->() RETURN 1"}}]
        )


def test_the_crosswalk_category_applies_only_with_identity_off() -> None:
    record = {
        "failure": "wrong set",
        "calls": [{"name": "run_sql", "index": 1, "input": {"query": "SELECT * FROM product__drug_mappings"}}],
    }
    off = next(c for c in CELLS if not c.identity)
    on = next(c for c in CELLS if c.identity)
    assert crosswalk_classifier(off)(record) == CROSSWALK
    assert crosswalk_classifier(on)(record) is None
    assert crosswalk_classifier(off)({**record, "failure": "budget exhausted"}) is None
    assert crosswalk_classifier(off)({**record, "calls": []}) is None
