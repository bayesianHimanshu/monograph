"""The spend guard: a test phase can never pass its approved cap, counting every earlier run and rerun (the H2.1
D1 overrun, USD 45.12 against USD 41.32, must not be possible). The fake client never calls the API."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from monograph.agent.arms import SUBMIT, Arm
from monograph.agent.budget import (
    BudgetedClient,
    BudgetExceeded,
    Cap,
    SpendGuard,
    load_cap,
    open_guard,
    spent,
    worst_case_cost,
)
from monograph.agent.llm import AGENT_SETTINGS, FakeToolClient, ToolRequest, tool_use
from monograph.extract.config import PRICES
from monograph.extract.llm import Usage
from monograph.qa.run import Writer, run_split

from .conftest import ROOT

HAIKU = "claude-haiku-5-5"


def _arm() -> Arm:
    return Arm(
        "S-free",
        "sql",
        "free",
        ({"name": "run_sql", "description": "", "input_schema": {}}, SUBMIT),
        lambda name, args: '{"rows": [[1]]}',
    )


def _script(request: ToolRequest) -> tuple[list[dict[str, Any]], str]:
    if len(request.messages) == 1:
        return [tool_use("run_sql", {"query": "SELECT 1"}, "t1")], "tool_use"
    return [tool_use("submit_answer", {"kind": "number", "number": 1, "evidence": [1]}, "t2")], "tool_use"


def _request(chars: int = 1000) -> ToolRequest:
    return ToolRequest(HAIKU, "s" * chars, (), ({"role": "user", "content": "q"},), AGENT_SETTINGS)


def test_the_worst_case_bounds_any_call() -> None:
    req = _request(5000)
    chars = 5000 + len(json.dumps([])) + len(json.dumps([{"role": "user", "content": "q"}]))
    p = PRICES[HAIKU]
    # tokens never outnumber characters: the dearest rate for every character, and the whole output budget
    for usage in (
        Usage(input_tokens=chars, output_tokens=AGENT_SETTINGS.max_tokens),
        Usage(cache_write_tokens=chars, output_tokens=AGENT_SETTINGS.max_tokens),
        Usage(cache_read_tokens=chars, output_tokens=AGENT_SETTINGS.max_tokens),
    ):
        assert usage.cost_usd(HAIKU) <= worst_case_cost(req)
    assert worst_case_cost(req) > 0
    assert p.output * AGENT_SETTINGS.max_tokens / Decimal(1_000_000) < worst_case_cost(req)


def test_a_call_that_could_pass_the_cap_is_refused() -> None:
    charged: list[Decimal] = []
    guard = SpendGuard(Cap("test", Decimal("1.00"), "x"), Decimal("0.95"), lambda a, i: charged.append(a))
    with pytest.raises(BudgetExceeded):
        guard.reserve(Decimal("0.06"))
    assert guard.tripped
    guard2 = SpendGuard(Cap("test", Decimal("1.00"), "x"), Decimal("0.90"), lambda a, i: charged.append(a))
    guard2.reserve(Decimal("0.06"))
    with pytest.raises(BudgetExceeded):  # the open reservation counts
        guard2.reserve(Decimal("0.06"))
    guard2.settle(Decimal("0.06"), Decimal("0.01"), {})
    assert guard2.spent == Decimal("0.91")
    assert charged == [Decimal("0.01")]


def test_concurrent_calls_never_pass_the_cap() -> None:
    cap = Decimal("1.00")
    guard = SpendGuard(Cap("test", cap, "x"), Decimal(0), lambda a, i: None)
    worst = Decimal("0.03")

    def work() -> None:
        for _ in range(200):
            try:
                guard.reserve(worst)
            except BudgetExceeded:
                return
            guard.settle(worst, worst, {})  # the worst case actually happens

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert guard.spent <= cap
    assert guard.tripped


def test_cumulative_spend_counts_every_run_and_rerun() -> None:
    def line(metric: str, **details: Any) -> dict[str, Any]:
        return {"metric": metric, "details": {"split": "test", **details}}

    records = [
        line("x.run", cost_usd="1.00", from_cache=False),  # an earlier run, before the ledger
        line("x.run", cost_usd="1.00", from_cache=False),  # its rerun: counted again
        line("x.run", cost_usd="5.00", from_cache=True),  # a cache replay: free
        line("x.run", cost_usd="2.00", from_cache=False, ledgered=True),  # its calls are in the spend lines
        line("x.spend", cost_usd="0.75"),
        line("x.spend", cost_usd="0.25"),
        {"metric": "x.spend", "details": {"split": "dev", "cost_usd": "9"}},  # another phase
    ]
    assert spent(records, "x", "test") == Decimal("3.00")


def test_the_h2_1_overrun_would_now_be_refused() -> None:
    results = ROOT / "results" / "h2_1"
    if not results.exists():
        pytest.skip("no H2.1 results in this copy")
    cap = load_cap(ROOT / "experiments" / "h2_1" / "budget.json", "test")
    assert cap.usd == Decimal("41.32")
    with pytest.raises(BudgetExceeded, match="has reached the approved cap"):
        open_guard(ROOT / "experiments" / "h2_1" / "budget.json", results, "h2_1", "test", lambda *a: None)


def test_no_cap_no_test_run(tmp_path: Path) -> None:
    with pytest.raises(BudgetExceeded, match="no approved budget"):
        load_cap(tmp_path / "budget.json", "test")
    (tmp_path / "budget.json").write_text('{"dev": {"cap_usd": "1", "approved": "x"}}')
    with pytest.raises(BudgetExceeded, match="for the test phase"):
        load_cap(tmp_path / "budget.json", "test")


def test_a_run_stops_at_the_cap_and_never_passes_it(tmp_path: Path) -> None:
    lines: list[tuple[str, Any, dict[str, Any]]] = []
    write = Writer(lambda metric, value, details: lines.append((metric, value, details)))
    budget = tmp_path / "budget.json"
    # one worst case (Haiku long-context output budget) is about USD 0.02; a question spends under USD 0.001
    budget.write_text('{"test": {"cap_usd": "0.03", "approved": "a test"}}')
    guard = open_guard(budget, tmp_path / "results", "x", "test", write)
    client = BudgetedClient(FakeToolClient(_script), guard)
    questions = [
        {"id": f"q{i}", "type": "T1", "template": "t", "text": "How many?", "gold": {"kind": "number", "number": 1}}
        for i in range(30)
    ]
    records = run_split(
        questions, "S-free", HAIKU, "test", _arm, "system", client, write, None, 1, metric="x", log=lambda _: None
    )
    assert 0 < len(records) < len(questions)
    assert guard.tripped
    assert guard.spent <= Decimal("0.03")
    metrics = [m for m, _, _ in lines]
    assert metrics.count("x.budget_stop") == 1
    assert "x.error" not in metrics
    charged = sum(Decimal(d["cost_usd"]) for m, _, d in lines if m == "x.spend")
    assert charged == guard.spent  # every API call is in the ledger
    # two calls per finished question, plus at most one by the question the cap stopped
    assert 2 * len(records) <= metrics.count("x.spend") <= 2 * len(records) + 1
