"""Arms A0 to A3 as composable steps over one conversation.

A0  first extraction only
A1  A0, then the Pydantic loop (record-local repair)
A2  A1, then deterministic repair (no LLM call)
A3  A2, then the graph-context loop (SHACL on the extraction merged into the reference graph)

Each loop is capped at MAX_REPAIR_ROUNDS model calls after the first extraction. Every model call is a
logged round. Arms share their common prefix of requests, so through the response cache the shared
rounds are byte-identical across arms.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from monograph.core.instances import Instantiator
from monograph.core.models import LabelExtraction
from monograph.extract.config import ARMS, Settings
from monograph.extract.llm import LlmClient, LlmRequest, LlmResponse, Message, Usage
from monograph.extract.prompts import PromptSet, user_message
from monograph.extract.repair.deterministic import Fix, repair
from monograph.extract.repair.pydantic_feedback import pydantic_feedback
from monograph.extract.repair.shacl_feedback import ContextIssue, GraphContextChecker, context_feedback
from monograph.extract.sources.dailymed import SplDocument, narrative_text
from monograph.validate.pydantic_side import PydanticValidator
from monograph.validate.verdict import Violation


@dataclass
class Round:
    index: int
    phase: str  # first | pydantic | graph
    feedback: str | None  # the feedback text that preceded this call (None for the first)
    request_key: str
    stop_reason: str
    usage: Usage
    latency_s: float
    from_cache: bool
    payload: Any  # after deterministic fixes, if any were applied to this round
    parse_error: str | None
    raw_payload: Any = None  # exactly as the model sent it
    violations: list[str] = field(default_factory=list)  # Pydantic verdict on this round's payload
    fixes: list[dict[str, str]] = field(default_factory=list)  # deterministic fixes applied after this round
    issues: list[dict[str, Any]] = field(default_factory=list)  # graph-context issues on this round's payload


@dataclass
class RecordResult:
    set_id: str
    version: int | None
    model: str
    arm: str
    rounds: list[Round]
    final_payload: Any
    status: str  # ok | invalid | invalid_json | truncated | refused
    pydantic_valid: bool
    context_issues: list[dict[str, Any]]  # graph-context check of the final payload (all arms, for reporting)

    @property
    def repair_rounds(self) -> int:
        return len(self.rounds) - 1

    def usage(self) -> Usage:
        return Usage(
            sum(r.usage.input_tokens for r in self.rounds),
            sum(r.usage.output_tokens for r in self.rounds),
            sum(r.usage.cache_read_tokens for r in self.rounds),
            sum(r.usage.cache_write_tokens for r in self.rounds),
        )


def plain(value: Any) -> Any:
    """JSON-safe copy (Decimals as strings) for logging."""
    return json.loads(json.dumps(value, default=str))


def parse_payload(text: str | None) -> tuple[Any, str | None]:
    if text is None:
        return None, "no text in the response"
    try:
        return json.loads(text, parse_float=Decimal), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


class Extractor:
    def __init__(
        self,
        client: LlmClient,
        prompts: PromptSet,
        model: str,
        settings: Settings,
        pydantic: PydanticValidator,
        instantiator: Instantiator,
        context: GraphContextChecker,
        max_rounds: int,
    ) -> None:
        self._client = client
        self._prompts = prompts
        self._model = model
        self._settings = settings
        self._pydantic = pydantic
        self._instantiator = instantiator
        self._context = context
        self._max_rounds = max_rounds

    # ------------------------------------------------------------------------------------------ run

    def run(self, doc: SplDocument, arm: str) -> RecordResult:
        if arm not in ARMS:
            raise ValueError(f"unknown arm {arm!r}")
        messages = [Message("user", user_message(narrative_text(doc)))]
        rounds: list[Round] = []
        last = self._call(messages, rounds, "first", None)

        if arm in ("A1", "A2", "A3"):
            last = self._pydantic_loop(doc, messages, rounds, last)
        if arm in ("A2", "A3"):
            self._deterministic(rounds[-1])
        if arm == "A3":
            last = self._graph_loop(doc, messages, rounds, last)
        return self._finish(doc, arm, rounds, last)

    # ---------------------------------------------------------------------------------------- steps

    def _call(self, messages: list[Message], rounds: list[Round], phase: str, feedback: str | None) -> LlmResponse:
        request = LlmRequest(self._model, self._prompts.system, self._prompts.schema, tuple(messages), self._settings)
        response = self._client.complete(request)
        payload, error = parse_payload(response.text)
        rounds.append(
            Round(
                len(rounds),
                phase,
                feedback,
                request.key(),
                response.stop_reason,
                response.usage,
                response.latency_s,
                response.from_cache,
                payload,
                error,
                raw_payload=copy.deepcopy(payload),
            )
        )
        return response

    def _continue(self, messages: list[Message], response: LlmResponse, feedback: str) -> bool:
        """Append the model's answer and the feedback; False if there is nothing to continue from."""
        if response.text is None or response.refused or response.truncated:
            return False
        messages += [Message("assistant", response.text), Message("user", feedback)]
        return True

    def _validate(self, doc: SplDocument, payload: Any) -> tuple[LabelExtraction | None, list[Violation]]:
        if not isinstance(payload, dict):
            return None, [Violation("<record>", "type", "the answer must be a JSON object")]
        record = {"set_id": doc.set_id, "version": doc.version, **payload}
        instance = self._instantiator.instantiate(LabelExtraction, record)
        verdict = self._pydantic.check(instance)
        if not verdict.valid:
            return None, list(verdict.violations)
        model = LabelExtraction.model_validate(instance, context=self._pydantic.context)
        return model, []

    def _pydantic_loop(
        self, doc: SplDocument, messages: list[Message], rounds: list[Round], last: LlmResponse
    ) -> LlmResponse:
        for _ in range(self._max_rounds):
            current = rounds[-1]
            _, violations = self._validate(doc, current.payload) if current.parse_error is None else (None, [])
            current.violations = [v.describe() for v in violations]
            if current.parse_error is None and not violations:
                break
            feedback = pydantic_feedback(violations, current.parse_error)
            if not self._continue(messages, last, feedback):
                break
            last = self._call(messages, rounds, "pydantic", feedback)
        else:
            current = rounds[-1]
            _, violations = self._validate(doc, current.payload) if current.parse_error is None else (None, [])
            current.violations = [v.describe() for v in violations]
        return last

    def _deterministic(self, current: Round) -> None:
        if current.payload is None:
            return
        repaired, fixes = repair(current.payload)
        current.payload = repaired
        current.fixes += [_fix(f) for f in fixes]

    def _graph_loop(
        self, doc: SplDocument, messages: list[Message], rounds: list[Round], last: LlmResponse
    ) -> LlmResponse:
        for _ in range(self._max_rounds):
            current = rounds[-1]
            model, violations = self._validate(doc, current.payload) if current.parse_error is None else (None, [])
            current.violations = [v.describe() for v in violations]
            if model is None:
                feedback = pydantic_feedback(violations, current.parse_error)
            else:
                _, issues = self._context.check(model)
                current.issues = [_issue(i) for i in issues]
                if not issues:
                    break
                feedback = context_feedback(issues)
            if not self._continue(messages, last, feedback):
                break
            last = self._call(messages, rounds, "graph", feedback)
            self._deterministic(rounds[-1])
        return last

    def _finish(self, doc: SplDocument, arm: str, rounds: list[Round], last: LlmResponse) -> RecordResult:
        final = rounds[-1]
        model, violations = self._validate(doc, final.payload) if final.parse_error is None else (None, [])
        if not final.violations:
            final.violations = [v.describe() for v in violations]
        context_issues: list[dict[str, Any]] = []
        if model is not None:
            _, issues = self._context.check(model)
            context_issues = [_issue(i) for i in issues]
        if last.refused:
            status = "refused"
        elif last.truncated:
            status = "truncated"
        elif final.parse_error is not None:
            status = "invalid_json"
        elif model is None:
            status = "invalid"
        else:
            status = "ok"
        return RecordResult(
            doc.set_id, doc.version, self._model, arm, rounds, final.payload, status, model is not None, context_issues
        )


def _fix(fix: Fix) -> dict[str, str]:
    return asdict(fix)


def _issue(issue: ContextIssue) -> dict[str, Any]:
    data = asdict(issue)
    data["candidates"] = list(issue.candidates)
    return data
