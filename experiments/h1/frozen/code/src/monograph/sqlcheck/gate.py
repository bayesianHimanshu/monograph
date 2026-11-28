"""`run_sql` with the ontology check in front of it, and the execution-error retry every arm gets.

One gate per question. Modes (docs/h1/PLAN.md, "How the check runs"):

* `enforce` (the OC arms): a query with violations is not executed; the feedback is returned instead, as one
  repair round. After `max_rounds` rounds a query with violations is executed as written, and its result
  carries the remaining violations. A blocked query is also executed in shadow: its result is recorded for
  the false-block measurement and never returned.
* `shadow` (every other arm): every query is checked and executed; the violations are recorded, never sent.

Every arm: a query that fails in the database returns its error; after `max_exec_errors` failed executions
(the first plus the retries) the gate executes nothing more and says so.

The feedback is built from the query text and the check's violations only. The gate never sees the question
or its gold.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from monograph.sqlcheck.check import CheckResult, OntologyChecker, Violation
from monograph.tools.freeform import QueryOut
from monograph.tools.registry import ToolError

Mode = Literal["enforce", "shadow"]
MAX_ROUNDS = 3
MAX_EXEC_ERRORS = 4  # the first failed execution plus 3 retries
SHADOW_ROWS = 200


@dataclass
class QueryEvent:
    index: int  # the gate's own count, 1-based
    query: str
    parsed: bool
    violations: list[dict[str, str]]
    blocked: bool  # enforce: not executed, feedback returned
    executed: bool  # executed and its result (or error) returned
    error: str | None = None
    columns: list[str] = field(default_factory=list)  # the result, returned or shadow
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    shadow: bool = False  # the result was obtained in shadow (blocked query)
    repair_round: int = 0  # the round this block was (1..max_rounds), 0 if not blocked


def feedback(violations: list[Violation], round_no: int, max_rounds: int) -> str:
    lines = [
        f"The ontology check found {len(violations)} problem(s) with this query, so it was not run"
        f" (repair round {round_no} of {max_rounds}). Fix them and run the corrected query.",
    ]
    for n, v in enumerate(violations, 1):
        lines.append(f"{n}. [{v.rule}] {v.fragment}\n   {v.message}")
    return "\n".join(lines)


class QueryGate:
    def __init__(
        self,
        checker: OntologyChecker,
        execute: Callable[[str], QueryOut],
        mode: Mode,
        max_rounds: int = MAX_ROUNDS,
        max_exec_errors: int = MAX_EXEC_ERRORS,
    ) -> None:
        self._checker = checker
        self._execute = execute
        self._mode = mode
        self._max_rounds = max_rounds
        self._max_errors = max_exec_errors
        self.events: list[QueryEvent] = []
        self.rounds = 0
        self.exec_errors = 0

    @property
    def retries_exhausted(self) -> bool:
        return self.exec_errors >= self._max_errors

    def _run(self, query: str) -> QueryOut:
        try:
            return self._execute(query)
        except ToolError:
            raise
        except Exception as exc:  # a driver failure is a query error for the model
            raise ToolError(f"query error: {exc}") from exc

    def run_sql(self, query: str) -> str:
        if self.retries_exhausted:
            raise ToolError(
                f"no execution retries left ({self._max_errors} queries failed); submit your answer with what you have"
            )
        result: CheckResult = self._checker.check(query)
        event = QueryEvent(
            len(self.events) + 1,
            query,
            result.parsed,
            [asdict(v) for v in result.violations],
            blocked=False,
            executed=False,
        )
        self.events.append(event)
        if self._mode == "enforce" and result.violations and self.rounds < self._max_rounds:
            self.rounds += 1
            event.blocked, event.repair_round, event.shadow = True, self.rounds, True
            try:
                out = self._run(query)
                event.columns, event.rows, event.truncated = out.columns, out.rows[:SHADOW_ROWS], out.truncated
            except ToolError as exc:
                event.error = str(exc)
            raise ToolError(feedback(result.violations, self.rounds, self._max_rounds))
        event.executed = True
        try:
            out = self._run(query)
        except ToolError as exc:
            event.error = str(exc)
            self.exec_errors += 1
            left = self._max_errors - self.exec_errors
            raise ToolError(f"{exc} ({left} execution retr{'y' if left == 1 else 'ies'} left)") from exc
        event.columns, event.rows, event.truncated = out.columns, out.rows[:SHADOW_ROWS], out.truncated
        text = out.model_dump_json()
        if self._mode == "enforce" and result.violations:
            note = {"note": "executed after the repair rounds ran out; the ontology check still reports:"}
            note["violations"] = "; ".join(f"[{v.rule}] {v.message}" for v in result.violations)
            text = json.dumps({**json.loads(text), **note}, ensure_ascii=False)
        return text

    def log(self) -> list[dict[str, Any]]:
        return [asdict(e) for e in self.events]
