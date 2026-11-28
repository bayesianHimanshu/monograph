"""The H1 decision rules on synthetic results: holds, fails (fails wins), partly holds, Sonnet's Q1 condition
from Haiku, the ceiling marks, and the check's precision, recall and false-block rate."""

from __future__ import annotations

from typing import Any

from monograph.h1 import analysis as a

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"


def _rec(model: str, arm: str, qid: str, qtype: str, correct: bool, **kw: Any) -> dict[str, Any]:
    return {
        "model": model,
        "arm": arm,
        "question_id": qid,
        "type": qtype,
        "correct": correct,
        "coverage": "inside" if qtype in ("Q1", "Q2", "Q5") else "outside",
        "failure": None if correct else "other wrong result",
        **kw,
    }


def _arm(model: str, arm: str, rates: dict[str, float], n: int = 40) -> list[dict[str, Any]]:
    out = []
    for qtype, rate in rates.items():
        k = round(rate * n)
        out += [_rec(model, arm, f"{qtype}-{i}", qtype, i < k) for i in range(n)]
    return out


def test_holds_for_one_model() -> None:
    runs = _arm(HAIKU, "SV", {"Q1": 0.9, "Q2": 0.5, "Q3": 0.2, "Q4": 0.3, "Q5": 0.4})
    runs += _arm(HAIKU, "ONT+OC", {"Q1": 0.88, "Q2": 0.8, "Q3": 0.6, "Q4": 0.5, "Q5": 0.7})
    p = a.primary(a.Table.of(runs))
    assert p["verdict"] == "holds"
    (c,) = p["comparisons"]
    assert c["difference"]["mean"] > 0.10
    assert c["difference"]["ci_low"] > 0


def test_q1_condition_blocks_holds() -> None:
    runs = _arm(HAIKU, "SV", {"Q1": 0.9, "Q2": 0.5, "Q3": 0.2, "Q4": 0.3, "Q5": 0.4})
    runs += _arm(HAIKU, "ONT+OC", {"Q1": 0.7, "Q2": 0.8, "Q3": 0.6, "Q4": 0.5, "Q5": 0.7})
    assert a.primary(a.Table.of(runs))["verdict"] == "partly holds"


def test_fails_when_sv_is_within_or_ahead_for_both_models() -> None:
    runs = []
    for model in (HAIKU, SONNET):
        runs += _arm(model, "SV", {"Q1": 0.9, "Q2": 0.7, "Q3": 0.5, "Q4": 0.5, "Q5": 0.7})
        runs += _arm(model, "ONT+OC", {"Q1": 0.9, "Q2": 0.6, "Q3": 0.55, "Q4": 0.5, "Q5": 0.65})
    assert a.primary(a.Table.of(runs))["verdict"] == "fails"


def test_fails_takes_precedence_and_sonnet_uses_haikus_q1() -> None:
    runs = _arm(HAIKU, "SV", {"Q1": 0.9, "Q2": 0.7, "Q3": 0.5, "Q4": 0.5, "Q5": 0.7})
    runs += _arm(HAIKU, "ONT+OC", {"Q1": 0.9, "Q2": 0.7, "Q3": 0.5, "Q4": 0.5, "Q5": 0.7})
    runs += _arm(SONNET, "SV", {"Q2": 0.5, "Q3": 0.2, "Q4": 0.3, "Q5": 0.4})
    runs += _arm(SONNET, "ONT+OC", {"Q2": 0.9, "Q3": 0.7, "Q4": 0.6, "Q5": 0.8})
    p = a.primary(a.Table.of(runs))
    sonnet = next(c for c in p["comparisons"] if c["model"] == SONNET)
    assert sonnet["q1_model"] == HAIKU
    assert sonnet["holds"]
    assert p["verdict"] == "holds"  # Sonnet holds and Haiku alone being within does not make it fail


def test_ceiling_marks() -> None:
    runs = _arm(HAIKU, "SV", {"Q1": 1.0, "Q6": 1.0, "Q2": 0.5})
    runs += _arm(HAIKU, "W-raw", {"Q1": 0.95, "Q6": 0.9, "Q2": 0.5})
    assert a.non_informative(a.Table.of(runs)) == ["Q1"]


def test_check_quality() -> None:
    rs = [
        _rec(HAIKU, "ONT", "1", "Q2", False, check_flagged=True),
        _rec(HAIKU, "ONT", "2", "Q2", True, check_flagged=True),
        _rec(HAIKU, "ONT", "3", "Q2", False, check_flagged=False),
        _rec(HAIKU, "ONT+OC", "1", "Q2", False, blocked_queries=2, false_blocks=1, gold_equivalent_blocks=1),
        _rec(HAIKU, "ONT+OC", "2", "Q2", True, blocked_queries=2, false_blocks=0, gold_equivalent_blocks=1),
    ]
    q = {(r["model"], r["arm"]): r for r in a.check_quality(a.Table.of(rs))}
    assert q[(HAIKU, "ONT")]["precision"] == 0.5
    assert q[(HAIKU, "ONT")]["recall"] == 0.5
    assert q[(HAIKU, "ONT+OC")]["false_block_rate"] == 0.25
    assert q[(HAIKU, "ONT+OC")]["gold_equivalent_blocks"] == 2
