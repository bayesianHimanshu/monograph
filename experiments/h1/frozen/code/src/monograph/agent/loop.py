"""A minimal tool-use loop: no framework, the client and the arm are injected.

Per question: the model gets the arm's system prompt and tools and the question; each response's tool
calls are executed and their results sent back, until the model calls submit_answer. Budgets are the same
for every arm (`AgentSettings`): data tool calls, model turns and output tokens. Thinking blocks and every
other content block are replayed unchanged. A refusal is recorded, never retried with another model.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError

from monograph.agent.arms import Arm, FinalAnswer
from monograph.agent.llm import AgentSettings, ToolClient, ToolRequest
from monograph.core.meta import DEFAULT_BASE
from monograph.extract.llm import Usage
from monograph.tools.registry import ToolError

NUDGE = "Call submit_answer to give your final answer."
PREVIEW_CHARS = 1500
IDS_SEEN_CAP = 300
_ID = re.compile(re.escape(DEFAULT_BASE) + r"[A-Za-z0-9._~:/?#@!$&'()*+,;=%\-]+")


def ids_in(text: str) -> list[str]:
    out = list(dict.fromkeys(m.group(0).rstrip('",.;)') for m in _ID.finditer(text)))
    return out[:IDS_SEEN_CAP]


@dataclass
class CallLog:
    index: int
    name: str
    input: dict[str, Any]
    is_error: bool
    latency_s: float
    result_chars: int
    preview: str
    ids_seen: list[str] = field(default_factory=list)  # ids in the full result, first 300


@dataclass
class Transcript:
    question_id: str
    arm: str
    model: str
    outcome: str = "no_answer"  # answered | no_answer | budget_exhausted | truncated | refusal | error
    answer: dict[str, Any] | None = None
    calls: list[CallLog] = field(default_factory=list)
    turns: int = 0
    usage: dict[str, int] = field(default_factory=lambda: asdict(Usage()))
    cost_usd: str = "0"
    llm_latency_s: float = 0.0
    tool_latency_s: float = 0.0
    from_cache: bool = True
    stop_reasons: list[str] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    final_text: str = ""

    @property
    def latency_s(self) -> float:
        return round(self.llm_latency_s + self.tool_latency_s, 3)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["latency_s"] = self.latency_s
        return out


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[result truncated at {limit} characters]"


class AgentLoop:
    def __init__(
        self,
        client: ToolClient,
        arm: Arm,
        model: str,
        system: str,
        settings: AgentSettings,
        clock: Callable[[], float] = time.monotonic,
        tag: str | None = None,
        answer_model: type[BaseModel] = FinalAnswer,
    ) -> None:
        """`tag` keys the response cache (default: the arm's name); H2.1 adds the cell, since tools arms send
        byte-identical requests in the two description cells. `answer_model` validates submit_answer (H1 has
        its own; default H2's)."""
        self._tag = tag if tag is not None else arm.name
        self._answer_model = answer_model
        self._client = client
        self._arm = arm
        self._model = model
        self._system = system
        self._settings = settings
        self._clock = clock

    def run(self, question_id: str, question: str) -> Transcript:
        s = self._settings
        t = Transcript(question_id, self._arm.name, self._model)
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        usage = Usage()
        cost = Decimal(0)
        nudged = False
        data_calls = 0
        while t.turns < s.max_turns:
            request = ToolRequest(self._model, self._system, self._arm.tools, tuple(messages), s, self._tag)
            response = self._client.complete(request)
            t.turns += 1
            t.stop_reasons.append(response.stop_reason)
            t.llm_latency_s += response.latency_s
            t.from_cache = t.from_cache and response.from_cache
            if response.request_id:
                t.request_ids.append(response.request_id)
            u = response.usage
            usage = Usage(
                usage.input_tokens + u.input_tokens,
                usage.output_tokens + u.output_tokens,
                usage.cache_read_tokens + u.cache_read_tokens,
                usage.cache_write_tokens + u.cache_write_tokens,
            )
            cost += u.cost_usd(self._model)
            t.final_text = response.text()[-2000:]
            if response.stop_reason == "refusal":
                t.outcome = "refusal"
                break
            if response.stop_reason == "max_tokens":
                t.outcome = "truncated"
                break
            messages.append({"role": "assistant", "content": response.content})
            uses = response.tool_uses()
            if not uses:
                if nudged:
                    t.outcome = "no_answer"
                    break
                nudged = True
                messages.append({"role": "user", "content": NUDGE})
                continue
            results: list[dict[str, Any]] = []
            for use in uses:
                name, arguments, use_id = str(use["name"]), dict(use.get("input") or {}), str(use["id"])
                if name == "submit_answer":
                    try:
                        answer = self._answer_model.model_validate(arguments)
                    except ValidationError as exc:
                        results.append(self._error(use_id, f"invalid answer: {exc.errors(include_url=False)}"))
                        continue
                    t.answer = answer.model_dump()
                    t.outcome = "answered"
                    results.append({"type": "tool_result", "tool_use_id": use_id, "content": "recorded"})
                    continue
                if data_calls >= s.max_tool_calls:
                    results.append(self._error(use_id, f"tool budget exhausted ({s.max_tool_calls} calls); {NUDGE}"))
                    continue
                data_calls += 1
                start = self._clock()
                try:
                    text, is_error = self._arm.execute(name, arguments), False
                except ToolError as exc:
                    text, is_error = f"error: {exc}", True
                elapsed = round(self._clock() - start, 3)
                t.tool_latency_s += elapsed
                t.calls.append(
                    CallLog(
                        data_calls, name, arguments, is_error, elapsed, len(text), text[:PREVIEW_CHARS], ids_in(text)
                    )
                )
                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": use_id,
                    "content": _cap(text, s.result_char_cap),
                }
                if is_error:
                    result["is_error"] = True
                results.append(result)
            if t.outcome == "answered":
                break
            if usage.output_tokens >= s.max_output_tokens:
                t.outcome = "budget_exhausted"
                break
            messages.append({"role": "user", "content": results})
        else:
            t.outcome = "budget_exhausted"
        t.usage = asdict(usage)
        t.cost_usd = str(cost)
        t.llm_latency_s = round(t.llm_latency_s, 3)
        t.tool_latency_s = round(t.tool_latency_s, 3)
        return t

    @staticmethod
    def _error(use_id: str, text: str) -> dict[str, Any]:
        return {"type": "tool_result", "tool_use_id": use_id, "content": text, "is_error": True}


def transcript_json(t: Transcript) -> str:
    return json.dumps(t.as_dict(), sort_keys=True, ensure_ascii=False)
