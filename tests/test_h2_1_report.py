"""H2.1 analysis and report on synthetic records whose verdicts are known in advance."""

from __future__ import annotations

from typing import Any

import pytest

from monograph.ablation import analysis
from monograph.ablation.analysis import Table
from monograph.ablation.cells import BASELINE, HARDEST
from monograph.ablation.report import render
from monograph.ablation.run import HAIKU, SONNET, configs

TYPES = {"T1": 6, "T3": 6, "T4": 6, "T9": 6, "T10": 6, "T11": 6, "T6": 6}


def _record(model: str, cell: str, arm: str, qid: str, qtype: str, right: bool, split: str = "test") -> dict[str, Any]:
    gold = {"kind": "ids", "ids": ["https://example.org/monograph/product/1"]}
    return {
        "model": model,
        "cell": cell,
        "arm": arm,
        "question_id": qid,
        "type": qtype,
        "template": {"T3": "t3_moiety_products", "T4": "t4_labelers_by_rxcui"}.get(qtype, f"{qtype.lower()}_x"),
        "split": split,
        "correct": right,
        "f1": 1.0 if right else 0.0,
        "failure": None if right else "wrong set",
        "question": f"Question {qid}?",
        "gold": gold,
        "answer": gold if right else {"kind": "none"},
        "calls": [
            {
                "index": 1,
                "name": "run_sql" if arm == "S-free" else "neighbors",
                "input": {"query": "x"},
                "is_error": False,
                "latency_s": 0.01,
                "preview": "",
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0},
        "cost_usd": "0.001",
        "latency_s": 1.0,
    }


def _records(rule: Any, split: str = "test") -> list[dict[str, Any]]:
    out = []
    for c in configs():
        for qtype, n in TYPES.items():
            for i in range(n):
                qid = f"{qtype}-{i}"
                out.append(_record(c.model, c.cell.name, c.arm, qid, qtype, rule(c, qtype, i), split))
    return out


def _wrapped(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"metric": "h2_1.run", "details": d} for d in details]


def test_identity_matters_descriptions_hit_free_form_and_the_graph_wins_deep_paths() -> None:
    def rule(c: Any, qtype: str, i: int) -> bool:
        if qtype in ("T3", "T4") and not c.cell.identity:
            return i == 0  # identity off: 1 of 6 right
        if not c.cell.descriptions and c.arm.endswith("free"):
            return i < 2  # descriptions off: free-form arms drop
        if c.cell == BASELINE and qtype in ("T9", "T10") and c.arm == "S-free":
            return i < 3  # SQL free-form is worse on deep paths
        return True

    t = Table.of(analysis.runs(_wrapped(_records(rule)), "test"))
    p1 = analysis.p1(t)
    assert p1["verdict"] == "confirmed"
    assert all(d["mean"] == pytest.approx(5 / 6) for d in p1["arms"].values())
    p2 = analysis.p2(t)
    assert p2["verdict"] == "holds"
    assert p2["drops"]["S-tools"]["mean"] == 0
    p3 = analysis.p3(t)
    assert p3["verdict"] == "holds"
    assert p3["models"][HAIKU]["mean"] == pytest.approx(0.5)
    assert analysis.refined(p3) == "holds"


def test_no_effects_refute_p1_and_fail_the_refined_hypothesis() -> None:
    t = Table.of(analysis.runs(_wrapped(_records(lambda c, q, i: True)), "test"))
    assert analysis.p1(t)["verdict"] == "refuted"
    assert analysis.p2(t)["verdict"] == "does not hold"
    p3 = analysis.p3(t)
    assert p3["verdict"] == "does not hold"
    assert set(p3["models"]) == {HAIKU, SONNET}
    assert analysis.refined(p3) == "fails"


def test_the_ceiling_rule_needs_every_arm_of_both_models() -> None:
    def rule(c: Any, qtype: str, i: int) -> bool:
        return not (qtype == "T9" and c.model == SONNET and c.arm == "S-free" and i == 0)

    dev = Table.of(analysis.runs(_wrapped(_records(rule, "dev")), "dev"))
    marked = analysis.non_informative(dev)
    assert "T9" not in marked  # one Sonnet arm under 0.95
    assert {"T1", "T10"} <= set(marked)


def test_the_latest_record_counts_once() -> None:
    a = _record(HAIKU, BASELINE.name, "S-free", "T1-0", "T1", False)
    b = _record(HAIKU, BASELINE.name, "S-free", "T1-0", "T1", True)
    details = analysis.runs(_wrapped([a, b]), "test")
    assert len(details) == 1
    assert details[0]["correct"] is True


def test_the_report_has_every_section_and_no_em_dash() -> None:
    def rule(c: Any, qtype: str, i: int) -> bool:
        return not (c.cell == HARDEST and i % 2 == 0)

    text = render(_records(rule), _records(lambda c, q, i: True, "dev"), "")
    for heading in (
        "## Verdicts against the PREREG",
        "## Runs",
        "## Identity x descriptions x arm, per type",
        "## P2 interaction data",
        "## Backend execution latency on deep paths (T9 to T11)",
        "## Failure categories",
        "## AGE limitations hit",
        "## Worked examples (hard types)",
        "## Cost and latency",
        "## What this means for the PoV",
    ):
        assert heading in text
    assert chr(0x2014) not in text  # the em-dash, written as a code point
    assert "Refined H2" in text
