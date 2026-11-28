"""A hard cap on API spend for one phase of an experiment (its test runs), across processes and reruns.

The cap is the one the user approved, committed in `experiments/<exp>/budget.json`:

    {"test": {"cap_usd": "41.32", "approved": "who approved what, and when"}}

Spend is cumulative for the phase: everything already spent (read from the experiment's results JSONL) plus
everything this process spends. `BudgetedClient` wraps the network client inside the response cache, so it sees
only real API calls. Before each call it reserves the call's worst-case cost (every input character as one
token at the dearest input rate, plus the full output budget at the output rate) and refuses the call if
spent + open reservations + this call would pass the cap; after the call it charges the actual cost and
appends one `<metric>.spend` line to the results. The cap therefore cannot be exceeded, a rerun counts like
any other run, and spend inside a question that later errors is counted too.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from monograph.agent.llm import ToolClient, ToolRequest, ToolResponse
from monograph.extract.config import PRICES

BUDGET_FILE = "budget.json"


class BudgetExceeded(RuntimeError):
    """The next API call could take cumulative spend past the approved cap."""


@dataclass(frozen=True, slots=True)
class Cap:
    phase: str
    usd: Decimal
    approved: str


def load_cap(path: Path, phase: str) -> Cap:
    """The approved cap for a phase; a missing file or phase is an error (no cap, no test run)."""
    if not path.exists():
        raise BudgetExceeded(f"no approved budget: {path} does not exist")
    entry = json.loads(path.read_text(encoding="utf-8")).get(phase)
    if not entry:
        raise BudgetExceeded(f"no approved budget for the {phase} phase in {path}")
    return Cap(phase, Decimal(str(entry["cap_usd"])), str(entry["approved"]))


def spent(records: Iterable[Mapping[str, Any]], metric: str, split: str) -> Decimal:
    """Cumulative API spend of a phase from the results: every `<metric>.spend` line (one per API call), plus,
    for runs recorded before the ledger existed, each run's cost when any of its responses was not cached (the
    whole run's cost, so history is counted high, never low)."""
    total = Decimal(0)
    for r in records:
        d = r.get("details", {})
        if d.get("split") != split:
            continue
        ledger = r.get("metric") == f"{metric}.spend"
        unledgered_run = r.get("metric") == f"{metric}.run" and not d.get("from_cache") and not d.get("ledgered")
        if ledger or unledgered_run:
            total += Decimal(str(d["cost_usd"]))
    return total


def worst_case_cost(request: ToolRequest) -> Decimal:
    """An upper bound on one call's cost: tokens never outnumber characters, the dearest input rate, and the full
    output budget."""
    p = PRICES[request.model]
    chars = len(request.system) + len(json.dumps(list(request.tools))) + len(json.dumps(list(request.messages)))
    input_rate = max(p.input, p.cache_write, p.cache_read)
    return (input_rate * chars + p.output * request.settings.max_tokens) / Decimal(1_000_000)


class SpendGuard:
    """Thread-safe: spent so far, open reservations, and the cap."""

    def __init__(self, cap: Cap, already_spent: Decimal, record: Callable[[Decimal, dict[str, Any]], None]) -> None:
        self._cap = cap
        self._spent = already_spent
        self._reserved = Decimal(0)
        self._record = record
        self._lock = threading.Lock()
        self.tripped = False  # a call was refused: the run stopped at the cap

    @property
    def cap(self) -> Cap:
        return self._cap

    @property
    def spent(self) -> Decimal:
        with self._lock:
            return self._spent

    def check_open(self) -> None:
        """Refuse to start when the phase has already reached its cap."""
        with self._lock:
            if self._spent >= self._cap.usd:
                raise BudgetExceeded(
                    f"{self._cap.phase} spend USD {self._spent:.2f} has reached the approved cap"
                    f" USD {self._cap.usd:.2f}"
                )

    def reserve(self, amount: Decimal) -> None:
        with self._lock:
            if self._spent + self._reserved + amount > self._cap.usd:
                self.tripped = True
                raise BudgetExceeded(
                    f"the next API call (up to USD {amount:.4f}) could take {self._cap.phase} spend past the approved"
                    f" cap USD {self._cap.usd:.2f} (spent USD {self._spent:.2f}, USD {self._reserved:.2f} reserved"
                    " by calls in flight)"
                )
            self._reserved += amount

    def settle(self, reserved: Decimal, actual: Decimal, info: dict[str, Any]) -> None:
        with self._lock:
            self._reserved -= reserved
            self._spent += actual
            self._record(actual, {**info, "spent_usd": str(self._spent), "cap_usd": str(self._cap.usd)})


class BudgetedClient:
    """Wraps the network client (inside the response cache): every real call is reserved, then charged."""

    def __init__(self, inner: ToolClient, guard: SpendGuard) -> None:
        self._inner = inner
        self._guard = guard

    def complete(self, request: ToolRequest) -> ToolResponse:
        worst = worst_case_cost(request)
        self._guard.reserve(worst)
        actual = Decimal(0)
        request_id = None
        try:
            response = self._inner.complete(request)
            actual = response.usage.cost_usd(request.model)
            request_id = response.request_id
            return response
        finally:
            self._guard.settle(worst, actual, {"model": request.model, "tag": request.tag, "request_id": request_id})


def open_guard(
    cap_file: Path,
    results_dir: Path,
    metric: str,
    split: str,
    write: Callable[[str, Any, dict[str, Any]], None],
) -> SpendGuard:
    """The guard for one run of a phase: the approved cap, the phase's cumulative spend so far (every earlier run
    and rerun), and one `<metric>.spend` line per API call from now on. Raises BudgetExceeded if the phase has
    already reached its cap."""
    from monograph.harness.results import read_results

    cap = load_cap(cap_file, split)
    already = spent(read_results(results_dir), metric, split) if results_dir.exists() else Decimal(0)

    def record(actual: Decimal, info: dict[str, Any]) -> None:
        write(f"{metric}.spend", float(actual), {"cost_usd": str(actual), "split": split, **info})

    guard = SpendGuard(cap, already, record)
    guard.check_open()
    return guard
