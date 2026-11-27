"""The LLM boundary for tool use: a Protocol, the Anthropic implementation, a content-addressed response
cache and a scripted fake.

Responses keep their full content blocks (text, thinking with signature, tool_use) so the loop can replay
them unchanged, as Sonnet 5.5 and Haiku 5.5 require, and so a cached replay is exact. The cache is
append-only: the key is SHA-256 over the model, system prompt, tools, messages and settings. The SDK reads
ANTHROPIC_API_KEY from the environment; nothing here logs it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from monograph.extract.llm import Usage


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Fixed for every arm. These models reject non-default sampling parameters, so "fixed sampling" is a
    fixed effort, the default (adaptive) thinking and a fixed output budget."""

    max_tokens: int = 8000  # per response
    effort: str = "medium"
    thinking: str = "adaptive (default; not sent)"
    max_tool_calls: int = 12  # data tool calls per question; submit_answer does not count
    max_turns: int = 16  # model responses per question
    max_output_tokens: int = 40000  # per question, all turns
    result_char_cap: int = 20000  # tool result text sent back to the model
    refusal_fallbacks: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


AGENT_SETTINGS = AgentSettings()


@dataclass(frozen=True, slots=True)
class ToolRequest:
    model: str
    system: str
    tools: tuple[dict[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
    settings: AgentSettings

    def key(self) -> str:
        payload = {
            "model": self.model,
            "system": self.system,
            "tools": list(self.tools),
            "messages": list(self.messages),
            "settings": self.settings.as_dict(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolResponse:
    content: list[dict[str, Any]]
    stop_reason: str
    usage: Usage
    latency_s: float
    model: str
    request_id: str | None = None
    from_cache: bool = False

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def text(self) -> str:
        return "\n".join(str(b.get("text", "")) for b in self.content if b.get("type") == "text")


class ToolClient(Protocol):
    def complete(self, request: ToolRequest) -> ToolResponse: ...


def _block(block: Any) -> dict[str, Any]:
    data: dict[str, Any] = block.model_dump(mode="json", exclude_none=True)
    return data


class AnthropicToolClient:
    """The real client. `sdk` is an `anthropic.Anthropic` instance, injected by the CLI."""

    def __init__(self, sdk: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self._sdk = sdk
        self._clock = clock

    def complete(self, request: ToolRequest) -> ToolResponse:
        start = self._clock()
        response = self._sdk.messages.create(
            model=request.model,
            max_tokens=request.settings.max_tokens,
            # The static prefix (tools, then system) gets an explicit breakpoint; the growing conversation
            # tail uses top-level automatic caching.
            system=[{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
            tools=list(request.tools),
            messages=list(request.messages),
            output_config={"effort": request.settings.effort},
            cache_control={"type": "ephemeral"},
        )
        latency = self._clock() - start
        usage = response.usage
        return ToolResponse(
            content=[_block(b) for b in response.content],
            stop_reason=str(response.stop_reason),
            usage=Usage(
                input_tokens=int(usage.input_tokens or 0),
                output_tokens=int(usage.output_tokens or 0),
                cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            ),
            latency_s=round(latency, 3),
            model=str(response.model),
            request_id=getattr(response, "_request_id", None),
        )


class ToolResponseCache:
    """`<dir>/<key[:2]>/<key>.json`, written once with O_EXCL and never replaced."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, key: str) -> Path:
        return self._directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> ToolResponse | None:
        path = self._path(key)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return ToolResponse(
            content=data["content"],
            stop_reason=data["stop_reason"],
            usage=Usage(**data["usage"]),
            latency_s=data["latency_s"],
            model=data["model"],
            request_id=data.get("request_id"),
            from_cache=True,
        )

    def put(self, key: str, response: ToolResponse) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {k: v for k, v in asdict(response).items() if k != "from_cache"}
        data = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


class CachedToolClient:
    def __init__(self, inner: ToolClient, cache: ToolResponseCache) -> None:
        self._inner = inner
        self._cache = cache
        self.network_calls = 0

    def complete(self, request: ToolRequest) -> ToolResponse:
        key = request.key()
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        self.network_calls += 1
        response = self._inner.complete(request)
        if response.stop_reason not in ("error",):
            self._cache.put(key, response)
        return response


Script = Callable[[ToolRequest], tuple[list[dict[str, Any]], str]]


@dataclass
class FakeToolClient:
    """Scripted responses for tests: `script(request)` returns the content blocks and the stop reason."""

    script: Script
    usage: Usage = field(default_factory=lambda: Usage(2000, 300, 1500, 500))
    requests: list[ToolRequest] = field(default_factory=list)

    def complete(self, request: ToolRequest) -> ToolResponse:
        self.requests.append(request)
        content, stop = self.script(request)
        return ToolResponse(content, stop, self.usage, 0.01, request.model)


def tool_use(name: str, arguments: dict[str, Any], block_id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments}


def scripted(turns: Sequence[list[dict[str, Any]] | tuple[list[dict[str, Any]], str]]) -> Script:
    """Answers in order, one turn per call. A bare list means stop_reason tool_use (or end_turn if no tool use)."""
    queue = list(turns)

    def script(request: ToolRequest) -> tuple[list[dict[str, Any]], str]:
        turn = queue.pop(0)
        if isinstance(turn, tuple):
            return turn
        stop = "tool_use" if any(b.get("type") == "tool_use" for b in turn) else "end_turn"
        return turn, stop

    return script
