"""Long-context rates, and the rate one request is billed at.

The standard rates are `PRICES` in `monograph.extract.config` (pinned by H4, so left as it is). This module adds
the second rate card some models have for long prompts, from Anthropic's pricing page
(https://platform.claude.com/docs/en/about-claude/pricing.md, read 2026-11-28):

- Claude Haiku 5.5 "is priced by prompt length: a request whose prompt is over 100,000 tokens pays higher
  prices. A request's prompt length counts all of its input tokens, including cache reads and cache writes.
  Each request is priced on its own." Over 100,000 tokens: input $0.50, 5-minute cache writes $0.625, cache
  hits $0.05, output $2.50 per million tokens.
- Claude Sonnet 5.5 is covered by "Claude 4.6 and later models (except Claude Haiku 5.5) ... include the full
  1M token context window at standard pricing": no second rate card.

A model in `PRICES` that is missing here has an unconfirmed long-context price. Its requests over
`UNCONFIRMED_THRESHOLD_TOKENS` are charged at the highest rate anywhere in the table, column by column, and
flagged, so the logged cost is high, never low.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from monograph.extract.config import HAIKU, PRICES, SONNET, Price

Tier = Literal["standard", "long_context", "unconfirmed_highest"]
SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing.md (read 2026-11-28)"


@dataclass(frozen=True, slots=True)
class LongContext:
    """A request whose prompt (input + cache read + cache write tokens) is over `threshold_tokens` pays `price`
    for all of its tokens, output included."""

    threshold_tokens: int
    price: Price


# None: confirmed standard pricing across the whole context window.
LONG_CONTEXT: dict[str, LongContext | None] = {
    SONNET: None,
    HAIKU: LongContext(100_000, Price(Decimal("0.50"), Decimal("2.50"), Decimal("0.625"), Decimal("0.05"))),
}

UNCONFIRMED_THRESHOLD_TOKENS = min(lc.threshold_tokens for lc in LONG_CONTEXT.values() if lc is not None)


def _every_price() -> list[Price]:
    return [*PRICES.values(), *(lc.price for lc in LONG_CONTEXT.values() if lc is not None)]


def highest() -> Price:
    """The dearest rate in the table, column by column."""
    prices = _every_price()
    return Price(
        max(p.input for p in prices),
        max(p.output for p in prices),
        max(p.cache_write for p in prices),
        max(p.cache_read for p in prices),
    )


@dataclass(frozen=True, slots=True)
class Rate:
    price: Price
    tier: Tier

    @property
    def confirmed(self) -> bool:
        return self.tier != "unconfirmed_highest"


def rate(model: str, prompt_tokens: int) -> Rate:
    """The rate one request with this many prompt tokens is billed at."""
    if model not in LONG_CONTEXT:
        if prompt_tokens > UNCONFIRMED_THRESHOLD_TOKENS:
            return Rate(highest(), "unconfirmed_highest")
        return Rate(PRICES[model], "standard")
    lc = LONG_CONTEXT[model]
    if lc is not None and prompt_tokens > lc.threshold_tokens:
        return Rate(lc.price, "long_context")
    return Rate(PRICES[model], "standard")


def dearest(model: str) -> Price:
    """The dearest rate a request to this model can be billed at, whatever its size (for worst-case bounds)."""
    if model not in LONG_CONTEXT:
        return highest()
    lc = LONG_CONTEXT[model]
    base = PRICES[model]
    if lc is None:
        return base
    return Price(
        max(base.input, lc.price.input),
        max(base.output, lc.price.output),
        max(base.cache_write, lc.price.cache_write),
        max(base.cache_read, lc.price.cache_read),
    )
