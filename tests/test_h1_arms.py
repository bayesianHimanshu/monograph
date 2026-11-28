"""The H1 arms end to end with a fake client and a fake database: shadow and enforced checks, repair rounds,
false blocks, failure categories, and the leak test (planted sentinel gold never reaches the model)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from monograph.agent.llm import FakeToolClient, scripted, tool_use
from monograph.h1 import arms, record
from monograph.h1.run import Config, configs, run_config
from monograph.qa.run import Writer
from monograph.tools.freeform import QueryOut
from monograph.tools.registry import ToolError

from .conftest import ROOT

BAD = "SELECT COUNT(*) FROM T_PRD_MSTR WHERE DSG_FRM_CD = 'TABLET'"
GOOD = "SELECT COUNT(*) FROM T_PRD_MSTR WHERE DSG_FRM_CD = 'C42998'"
SV_BAD = "SELECT COUNT(*) FROM products WHERE dosage_form = 'Tablet'"


def _q(gold: dict[str, Any], qtype: str = "Q1", **kw: Any) -> dict[str, Any]:
    return {
        "id": "q-01",
        "type": qtype,
        "template": "t",
        "text": "How many products have the dosage form TABLET?",
        "gold": gold,
        "coverage": "inside",
        "codes": {"Product.dosage_form": ["TABLET"]},
        "identity": [],
        **kw,
    }


def _db(answers: dict[str, list[list[Any]]]) -> Callable[[], Callable[[str], QueryOut]]:
    def execute(query: str) -> QueryOut:
        if query not in answers:
            raise ToolError("query error: relation does not exist")
        return QueryOut(columns=["n"], rows=answers[query])

    return lambda: execute


def _run(
    arm: str, turns: list[Any], db: dict[str, list[list[Any]]], q: dict[str, Any]
) -> tuple[dict[str, Any], FakeToolClient]:
    lines: list[tuple[str, Any, dict[str, Any]]] = []
    client = FakeToolClient(scripted(turns))
    (rec,) = run_config(
        ROOT,
        [q],
        Config("claude-haiku-5-5", arm),
        "dev",
        client,
        Writer(lambda m, v, d: lines.append((m, v, d))),
        _db(db),
        workers=1,
        log=lambda _: None,
    )
    assert [m for m, _, _ in lines] == ["h1.run"]
    return rec, client


def _turns(*queries: str, answer: dict[str, Any]) -> list[Any]:
    out: list[Any] = [[tool_use("describe_data", {}, "c0")]]
    out += [[tool_use("run_sql", {"query": q}, f"c{i}")] for i, q in enumerate(queries, 1)]
    out.append([tool_use("submit_answer", answer, "done")])
    return out


def test_shadow_check_records_but_never_blocks() -> None:
    q = _q({"kind": "number", "number": 7.0})
    rec, client = _run("ONT", _turns(BAD, answer={"kind": "number", "number": 0, "evidence": [2]}), {BAD: [[0]]}, q)
    assert not rec["correct"]
    assert rec["blocked_queries"] == 0
    assert rec["check_flagged"]
    assert rec["check_rules"] == ["code"]
    assert rec["failure"] == "wrong or invalid code"
    results = [
        b
        for r in client.requests
        for m in r.messages
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
    ]
    assert not any("ontology check" in str(b.get("content")) for b in results)


def test_enforced_check_blocks_and_the_repair_is_correct() -> None:
    q = _q({"kind": "number", "number": 7.0})
    rec, client = _run(
        "ONT+OC",
        _turns(BAD, GOOD, answer={"kind": "number", "number": 7, "evidence": [3]}),
        {BAD: [[0]], GOOD: [[7]]},
        q,
    )
    assert rec["correct"]
    assert rec["repair_rounds"] == 1
    assert rec["blocked_queries"] == 1
    assert rec["false_blocks"] == 0
    assert rec["failure"] is None
    assert not rec["check_flagged"]  # the evidence query is the repaired one
    sent = json.dumps([r.messages for r in client.requests])
    assert "repair round 1 of 3" in sent
    assert "'C42998' (TABLET)" in sent


def test_a_false_block_is_counted() -> None:
    # the check blocks COUNT(*) over the route table (a mention count), whose result was in fact the gold; the
    # agent then answers something else
    q = _q({"kind": "number", "number": 3.0}, codes={})
    flagged = "SELECT COUNT(*) FROM T_PRD_RTE WHERE RTE_CD = 'C38288'"
    other = "SELECT COUNT(DISTINCT PRD_SK) FROM T_PRD_RTE WHERE RTE_CD = 'C38288'"
    rec, _ = _run(
        "ONT+OC",
        _turns(flagged, other, answer={"kind": "number", "number": 2, "evidence": [3]}),
        {flagged: [[3]], other: [[2]]},
        q,
    )
    assert rec["blocked_queries"] == 1
    assert rec["gold_equivalent_blocks"] == 1
    assert rec["false_blocks"] == 1
    assert not rec["correct"]


def test_sv_bad_label_is_a_code_failure() -> None:
    q = _q({"kind": "number", "number": 7.0})
    rec, _ = _run("SV", _turns(SV_BAD, answer={"kind": "number", "number": 0, "evidence": [2]}), {SV_BAD: [[0]]}, q)
    assert rec["failure"] == "wrong or invalid code"
    assert rec["context"] == "SV"


SENTINELS = ("SENTINEL-GOLD-KEY-71", "SENTINEL-GOLD-NAME-72")


@pytest.mark.parametrize("arm", ["SV+OC", "ONT+OC"])
def test_leak_planted_gold_never_reaches_the_model(arm: str) -> None:
    """Gold holds sentinels; the agent's queries break every rule, are blocked three times and finally run.
    Nothing sent to the model (prompt, tools, tool results, feedback) contains a sentinel."""
    gold = {"kind": "values", "entities": [{"key": SENTINELS[0], "names": [SENTINELS[1]]}]}
    q = _q(gold, qtype="Q2", codes={"Product.dosage_form": ["TABLET"], "Product.routes": ["ORAL"]})
    bad = [
        "SELECT p.PRD_CD FROM T_PRD_MSTR p JOIN T_PKG k ON k.PRD_SK = p.PRD_CD WHERE p.DSG_FRM_CD = 'TABLET'",
        "SELECT COUNT(*) FROM T_PRD_MSTR p JOIN T_PRD_RTE r ON r.PRD_SK = p.PRD_SK WHERE r.RTE_CD = 'ORAL'",
        "SELECT p.NOPE FROM T_PRD_MSTR p",
        "SELECT p.PRD_CD FROM T_PRD_MSTR p, T_ORG_MSTR o WHERE p.DSG_FRM_CD = 'C42998'",
    ]
    if arm == "SV+OC":
        bad = [x.replace("T_PRD_MSTR", "wh.T_PRD_MSTR") for x in bad]
    db = {x: [[SENTINELS[0]]] for x in bad[:3]}  # the blocked queries' shadow results hold the gold
    db[bad[3]] = [["0002-1111"]]
    rec, client = _run(arm, _turns(*bad, answer={"kind": "values", "values": ["0002-1111"], "evidence": [5]}), db, q)
    assert rec["blocked_queries"] == 3
    sent = json.dumps([[r.system, r.tools, r.messages] for r in client.requests])
    for s in SENTINELS:
        assert s not in sent
    assert rec["gold_equivalent_blocks"] == 3  # the shadow results were recorded, and scored, but not sent


def test_failure_categories() -> None:
    base = {"arm": "ONT", "outcome": "answered", "answer": {"kind": "number", "number": 9, "evidence": []}}
    q = _q({"kind": "number", "number": 7.0}, codes={})
    ev = {"index": 1, "query": "SELECT 1", "violations": [], "blocked": False, "executed": True, "error": None}
    assert record.failure({**base, "outcome": "refusal"}, q) == "refusal"
    assert record.failure({**base, "outcome": "budget_exhausted"}, q) == "budget exhausted"
    assert record.failure({**base, "retries_exhausted": True, "queries": []}, q) == "SQL execution error after retries"
    halluc = {**ev, "violations": [{"rule": "existence", "fragment": "x", "message": "There is no table 'X'."}]}
    assert record.failure({**base, "queries": [halluc]}, q) == "hallucinated table or column"
    sv = {**base, "arm": "SV", "queries": [{**ev, "query": "SELECT COUNT(*) FROM products"}]}
    assert record.failure(sv, {**q, "coverage": "outside"}) == "outside-coverage miss"
    ident = {**base, "queries": [ev]}
    assert record.failure(ident, {**q, "identity": ["Substance.active_moiety"]}) == "identity miss"
    fan = {**base, "queries": [{**ev, "violations": [{"rule": "cardinality", "fragment": "", "message": ""}]}]}
    assert record.failure(fan, q) == "fan-out double count"
    over = {**base, "queries": [ev]}
    assert record.failure(over, {**q, "type": "Q5"}) == "fan-out double count"
    join = {**base, "queries": [{**ev, "violations": [{"rule": "join", "fragment": "", "message": ""}]}]}
    assert record.failure(join, q) == "wrong join path"
    assert record.failure({**base, "queries": [ev]}, q) == "other wrong result"


def test_every_arm_gets_the_same_base_prompt_tools_and_settings() -> None:
    base = arms.system_prompt("W-raw").split("\n\nYour tools")[0]
    for arm in arms.ARMS:
        assert arms.system_prompt(arm).startswith(base)
        assert ("checked against the ontology" in arms.system_prompt(arm)) == arm.endswith("+OC")
    assert arms.SETTINGS.max_tool_calls == 12
    assert [t["name"] for t in arms.TOOLS] == ["describe_data", "run_sql", "submit_answer"]


def test_configurations() -> None:
    cs = {(c.model, c.arm) for c in configs()}
    assert len(cs) == 8
    assert ("claude-sonnet-5-5", "ONT+OC") in cs
    assert ("claude-sonnet-5-5", "ONT") not in cs
