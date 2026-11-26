"""The LLM boundary: a small Protocol, the Anthropic implementation, a response cache and a fake.

Arms depend only on `LlmClient`. Tests and CI use `FakeClient` (scripted responses); `AnthropicClient`
is built only by the CLI, and the SDK reads ANTHROPIC_API_KEY from the environment. Nothing here logs a
credential.

The response cache is content-addressed and append-only: the key is SHA-256 over the model, the full
request and the settings, and an entry, once written, is never replaced. Reruns cost nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from monograph.extract.config import PRICES, Settings


@dataclass(frozen=True, slots=True)
class Message:
    role: str  # user | assistant
    text: str


@dataclass(frozen=True, slots=True)
class LlmRequest:
    model: str
    system: str  # the cached prefix: instructions and the schema
    schema: dict[str, Any]  # sent as output_config.format; already passed through anthropic.transform_schema
    messages: tuple[Message, ...]
    settings: Settings

    def key(self) -> str:
        payload = {
            "model": self.model,
            "system": self.system,
            "schema": self.schema,
            "messages": [asdict(m) for m in self.messages],
            "settings": self.settings.as_dict(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost_usd(self, model: str) -> Decimal:
        p = PRICES[model]
        million = Decimal(1_000_000)
        return (
            p.input * self.input_tokens
            + p.output * self.output_tokens
            + p.cache_read * self.cache_read_tokens
            + p.cache_write * self.cache_write_tokens
        ) / million


@dataclass(frozen=True, slots=True)
class LlmResponse:
    text: str | None
    stop_reason: str
    usage: Usage
    latency_s: float
    model: str
    request_id: str | None = None
    from_cache: bool = False

    @property
    def truncated(self) -> bool:
        return self.stop_reason == "max_tokens"

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


class LlmClient(Protocol):
    def complete(self, request: LlmRequest) -> LlmResponse: ...


class AnthropicClient:
    """The real client. `sdk` is an `anthropic.Anthropic` instance, injected by the CLI."""

    def __init__(self, sdk: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self._sdk = sdk
        self._clock = clock

    def complete(self, request: LlmRequest) -> LlmResponse:
        start = self._clock()
        response = self._sdk.messages.create(
            model=request.model,
            max_tokens=request.settings.max_tokens,
            system=[{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": m.role, "content": m.text} for m in request.messages],
            output_config={
                "effort": request.settings.effort,
                "format": {"type": "json_schema", "schema": request.schema},
            },
        )
        latency = self._clock() - start
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
        usage = response.usage
        return LlmResponse(
            text=text,
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


class ResponseCache:
    """`<dir>/<key[:2]>/<key>.json`, written once with O_EXCL and never replaced."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, key: str) -> Path:
        return self._directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> LlmResponse | None:
        path = self._path(key)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return LlmResponse(
            text=data["text"],
            stop_reason=data["stop_reason"],
            usage=Usage(**data["usage"]),
            latency_s=data["latency_s"],
            model=data["model"],
            request_id=data.get("request_id"),
            from_cache=True,
        )

    def put(self, key: str, response: LlmResponse) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {k: v for k, v in asdict(response).items() if k != "from_cache"}
        data = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return  # content-addressed: an existing entry for this key is the same request's answer
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


class CachedClient:
    """Answers from the cache when it can; otherwise asks the wrapped client and stores the answer."""

    def __init__(self, inner: LlmClient, cache: ResponseCache) -> None:
        self._inner = inner
        self._cache = cache
        self.network_calls = 0

    def complete(self, request: LlmRequest) -> LlmResponse:
        key = request.key()
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        self.network_calls += 1
        response = self._inner.complete(request)
        self._cache.put(key, response)
        return response


@dataclass
class FakeClient:
    """Scripted responses for tests: `script(request)` returns the text and stop reason to answer with."""

    script: Callable[[LlmRequest], tuple[str | None, str]]
    usage: Usage = field(default_factory=lambda: Usage(1000, 200, 0, 0))
    requests: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        text, stop = self.script(request)
        return LlmResponse(text, stop, self.usage, 0.01, request.model)


def scripted(answers: Sequence[str | tuple[str | None, str]]) -> Callable[[LlmRequest], tuple[str | None, str]]:
    """A script that answers in order, one answer per call."""
    queue = list(answers)

    def script(request: LlmRequest) -> tuple[str | None, str]:
        answer = queue.pop(0)
        return (answer, "end_turn") if isinstance(answer, str) else answer

    return script
