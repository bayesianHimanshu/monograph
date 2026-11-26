"""H4 configuration: pinned models, fixed request settings, prices. Recorded on every result line."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
MODELS: tuple[str, ...] = (SONNET, HAIKU)

ARMS: tuple[str, ...] = ("A0", "A1", "A2", "A3")
MAX_REPAIR_ROUNDS = 3  # per repair loop, after the first extraction


@dataclass(frozen=True, slots=True)
class Settings:
    """Fixed request settings. These models reject non-default temperature, top_p and top_k, so
    "fixed sampling" means a fixed effort, a fixed thinking mode and a fixed output budget."""

    max_tokens: int = 16000
    effort: str = "medium"
    thinking: str = "adaptive (default; not sent)"
    refusal_fallbacks: bool = False  # a fallback would answer with another model inside a one-model run

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


SETTINGS = Settings()


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens. Cache writes use the 5-minute TTL (1.25x input), reads 0.1x input."""

    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal


# Anthropic first-party prices (claude-api skill model table, cached 2026-11-03). Haiku 5.5 prices hold
# for prompts up to 100K tokens; H4 prompts are far below that.
PRICES: dict[str, Price] = {
    SONNET: Price(Decimal("2.00"), Decimal("10.00"), Decimal("2.50"), Decimal("0.10")),
    HAIKU: Price(Decimal("0.10"), Decimal("0.50"), Decimal("0.125"), Decimal("0.01")),
}

COST_APPROVAL_THRESHOLD_USD = Decimal("25")
