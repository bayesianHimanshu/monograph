"""Long-context rates: a request is billed by its own prompt size (input + cache read + cache write tokens), on
the rate card Anthropic's pricing page gives for it; a model whose long-context price is unconfirmed is charged
the highest rate in the table and flagged."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from monograph.agent.budget import BudgetedClient, Cap, SpendGuard, worst_case_cost
from monograph.agent.llm import AGENT_SETTINGS, ToolRequest, ToolResponse
from monograph.extract import config, pricing
from monograph.extract.config import HAIKU, PRICES, SONNET, Price
from monograph.extract.llm import Usage
from monograph.extract.pricing import LONG_CONTEXT, highest

M = Decimal(1_000_000)


def test_the_rates_are_the_pricing_page_values() -> None:
    lc = LONG_CONTEXT[HAIKU]
    assert lc is not None
    assert lc.threshold_tokens == 100_000
    assert lc.price == Price(Decimal("0.50"), Decimal("2.50"), Decimal("0.625"), Decimal("0.05"))
    assert LONG_CONTEXT[SONNET] is None  # standard pricing across the full window
    assert set(LONG_CONTEXT) == set(PRICES)  # every model in the table has a confirmed answer


def test_a_haiku_call_just_over_the_threshold_pays_the_long_context_rate() -> None:
    # 100,001 prompt tokens, split across plain input, cache reads and cache writes: they all count
    over = Usage(input_tokens=40_001, cache_read_tokens=50_000, cache_write_tokens=10_000, output_tokens=2_000)
    at = Usage(input_tokens=40_000, cache_read_tokens=50_000, cache_write_tokens=10_000, output_tokens=2_000)
    assert over.prompt_tokens == 100_001
    assert at.prompt_tokens == 100_000
    assert over.rate(HAIKU).tier == "long_context"
    assert at.rate(HAIKU).tier == "standard"  # "over 100,000 tokens": exactly 100,000 is standard
    assert (
        over.cost_usd(HAIKU)
        == (Decimal("0.50") * 40_001 + Decimal("0.05") * 50_000 + Decimal("0.625") * 10_000 + Decimal("2.50") * 2_000)
        / M
    )
    assert (
        at.cost_usd(HAIKU)
        == (Decimal("0.10") * 40_000 + Decimal("0.01") * 50_000 + Decimal("0.125") * 10_000 + Decimal("0.50") * 2_000)
        / M
    )
    # the H2.1 call of 103,511 prompt tokens would now be charged five times the standard rate
    assert Usage(input_tokens=103_511).cost_usd(HAIKU) == Decimal("0.50") * 103_511 / M


def test_sonnet_has_no_long_context_premium() -> None:
    big = Usage(input_tokens=900_000, output_tokens=1_000)
    assert big.rate(SONNET).tier == "standard"
    assert big.cost_usd(SONNET) == (Decimal("2.00") * 900_000 + Decimal("10.00") * 1_000) / M


@pytest.fixture
def unconfirmed(monkeypatch: pytest.MonkeyPatch) -> str:
    """A model with standard prices but no confirmed long-context price."""
    model = "claude-unconfirmed-test"
    prices = {**PRICES, model: Price(Decimal("1"), Decimal("4"), Decimal("1.25"), Decimal("0.1"))}
    monkeypatch.setattr(config, "PRICES", prices)
    monkeypatch.setattr(pricing, "PRICES", prices)
    return model


def test_an_unconfirmed_model_over_the_threshold_is_charged_the_highest_rate(unconfirmed: str) -> None:
    top = highest()
    assert top == Price(Decimal("2.00"), Decimal("10.00"), Decimal("2.50"), Decimal("0.10"))
    under = Usage(input_tokens=100_000, output_tokens=10)
    over = Usage(input_tokens=100_001, output_tokens=10)
    assert under.rate(unconfirmed).tier == "standard"
    assert under.cost_usd(unconfirmed) == (Decimal("1") * 100_000 + Decimal("4") * 10) / M
    assert over.rate(unconfirmed).tier == "unconfirmed_highest"
    assert not over.rate(unconfirmed).confirmed
    assert over.cost_usd(unconfirmed) == (top.input * 100_001 + top.output * 10) / M


class _Fixed:
    def __init__(self, usage: Usage) -> None:
        self._usage = usage

    def complete(self, request: ToolRequest) -> ToolResponse:
        return ToolResponse([], "end_turn", self._usage, 0.0, request.model, "req_1")


def _spend_line(model: str, usage: Usage) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    guard = SpendGuard(Cap("test", Decimal("100"), "x"), Decimal(0), lambda a, i: lines.append({"cost": a, **i}))
    request = ToolRequest(model, "s" * 10, (), ({"role": "user", "content": "q"},), AGENT_SETTINGS)
    BudgetedClient(_Fixed(usage), guard).complete(request)
    assert len(lines) == 1
    return lines[0]


def test_the_spend_line_records_the_rate_and_flags_an_unconfirmed_one(unconfirmed: str) -> None:
    line = _spend_line(HAIKU, Usage(input_tokens=100_001))
    assert line["rate"] == "long_context"
    assert line["prompt_tokens"] == 100_001
    assert "rate_unconfirmed" not in line
    assert line["cost"] == Decimal("0.50") * 100_001 / M
    assert _spend_line(HAIKU, Usage(input_tokens=5))["rate"] == "standard"
    flagged = _spend_line(unconfirmed, Usage(input_tokens=100_001))
    assert flagged["rate"] == "unconfirmed_highest"
    assert flagged["rate_unconfirmed"] is True


def test_the_worst_case_covers_the_long_context_rate() -> None:
    chars = 150_000
    request = ToolRequest(HAIKU, "s" * chars, (), ({"role": "user", "content": "q"},), AGENT_SETTINGS)
    worst_billed = Usage(input_tokens=chars, output_tokens=AGENT_SETTINGS.max_tokens).cost_usd(HAIKU)
    assert worst_billed <= worst_case_cost(request)
