"""H2 scoring: exact match, F1, failure categories, the verdict rule and the attribution sentence."""

from __future__ import annotations

from typing import Any

import pytest

from monograph.core.meta import DEFAULT_BASE
from monograph.qa.score import Gold, aggregate, failure_category, score_answer, tools_not_graph, verdict

B = DEFAULT_BASE
F1, F2, F3 = f"{B}organization/a", f"{B}organization/b", f"{B}organization/c"


def test_exact_set_match_accepts_short_ids() -> None:
    gold = Gold("ids", frozenset({F1, F2}))
    assert score_answer({"kind": "ids", "ids": ["organization/a", F2]}, gold) == (True, 1.0)
    ok, f1 = score_answer({"kind": "ids", "ids": [F1]}, gold)
    assert not ok
    assert f1 == pytest.approx(2 / 3)


def test_numbers_and_none() -> None:
    assert score_answer({"kind": "number", "number": 3}, Gold("number", number=3))[0]
    assert not score_answer({"kind": "number", "number": 4}, Gold("number", number=3))[0]
    assert score_answer({"kind": "none"}, Gold("none"))[0]
    assert score_answer({"kind": "ids", "ids": []}, Gold("none"))[0]
    assert not score_answer({"kind": "ids", "ids": [F1]}, Gold("none"))[0]
    assert score_answer(None, Gold("none")) == (False, 0.0)


def _record(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "correct": False,
        "outcome": "answered",
        "answer": {"kind": "ids", "ids": [F3]},
        "calls": [],
    }
    return {**base, **kw}


def test_failure_categories_in_order() -> None:
    gold = Gold("ids", frozenset({F1}))
    known = {F1, F2, F3, f"{B}substance/X"}
    anchor = [f"{B}substance/X"]
    seen = [{"input": {}, "ids_seen": [f"{B}substance/X"]}]
    assert failure_category(_record(correct=True), gold, anchor, known) is None
    assert failure_category(_record(outcome="refusal"), gold, anchor, known) == "refusal"
    assert failure_category(_record(outcome="budget_exhausted", answer=None), gold, anchor, known) == "budget exhausted"
    assert failure_category(_record(answer={"kind": "ids", "ids": [f"{B}organization/zz"]}), gold, anchor, known) == (
        "hallucinated id"
    )
    assert failure_category(_record(), gold, anchor, known) == "wrong entity resolution"
    errs = [{"input": {}, "ids_seen": [f"{B}substance/X"], "is_error": True}]
    assert failure_category(_record(calls=errs), gold, anchor, known) == "query error or timeout"
    hop = _record(calls=seen, answer={"kind": "ids", "ids": [f"{B}substance/X"]})
    assert failure_category(hop, gold, anchor, known) == "missing hop"
    assert failure_category(_record(calls=seen), gold, anchor, known) == "wrong set"


def _records(acc: dict[tuple[str, str, str], list[int]]) -> list[dict[str, Any]]:
    out = []
    for (model, arm, qtype), marks in acc.items():
        for i, m in enumerate(marks):
            out.append(
                {
                    "model": model,
                    "arm": arm,
                    "type": qtype,
                    "question_id": f"{qtype}-{i}",
                    "correct": bool(m),
                    "f1": float(m),
                    "calls": [],
                    "latency_s": 1.0,
                    "cost_usd": "0.01",
                    "usage": {},
                }
            )
    return out


def _cells(g_core: list[int], s_core: list[int], g_t1: list[int], s_t1: list[int], models: tuple[str, ...]) -> Any:
    acc: dict[tuple[str, str, str], list[int]] = {}
    for m in models:
        for t in ("T2", "T3", "T4"):
            acc[(m, "G-tools", t)] = g_core
            acc[(m, "S-free", t)] = s_core
        acc[(m, "G-tools", "T1")] = g_t1
        acc[(m, "S-free", "T1")] = s_t1
    return aggregate(_records(acc))


def test_verdict_holds_fails_and_partly() -> None:
    models = ("m1", "m2")
    holds = _cells([1] * 40, [1] * 20 + [0] * 20, [1] * 40, [1] * 40, models)
    assert verdict(holds, models)["verdict"] == "holds"
    fails = _cells([1] * 30 + [0] * 10, [1] * 29 + [0] * 11, [1] * 40, [1] * 40, models)
    assert verdict(fails, models)["verdict"] == "fails"
    t1_worse = _cells([1] * 40, [1] * 20 + [0] * 20, [1] * 30 + [0] * 10, [1] * 40, models)
    assert verdict(t1_worse, models)["verdict"] == "partly holds"


def test_tools_not_graph_sentence() -> None:
    zero = {"mean": 0.0, "lo": -0.05, "hi": 0.05}
    clear = {"mean": 0.3, "lo": 0.2, "hi": 0.4}
    attr = {
        "store: G-tools - S-tools": zero,
        "store: G-free - S-free": zero,
        "interface: S-tools - S-free": clear,
        "interface: G-tools - G-free": clear,
    }
    assert tools_not_graph(attr)
    assert not tools_not_graph({**attr, "interface: S-tools - S-free": zero, "interface: G-tools - G-free": zero})
