from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from monograph.extract.config import HAIKU, SETTINGS, SONNET
from monograph.extract.llm import (
    AnthropicClient,
    CachedClient,
    FakeClient,
    LlmRequest,
    Message,
    ResponseCache,
    Usage,
    scripted,
)


def _request(text: str = "narrative", model: str = SONNET) -> LlmRequest:
    return LlmRequest(model, "SYSTEM", {"type": "object"}, (Message("user", text),), SETTINGS)


class _StubMessages:
    def __init__(self, stop: str = "end_turn") -> None:
        self.calls: list[dict[str, Any]] = []
        self._stop = stop

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        usage = SimpleNamespace(
            input_tokens=120, output_tokens=40, cache_read_input_tokens=3000, cache_creation_input_tokens=0
        )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"products": []}')],
            stop_reason=self._stop,
            usage=usage,
            model=kwargs["model"],
            _request_id="req_test",
        )


def test_request_shape_and_usage_mapping() -> None:
    messages = _StubMessages()
    response = AnthropicClient(SimpleNamespace(messages=messages)).complete(_request())
    [call] = messages.calls
    assert call["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert call["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert {"temperature", "fallbacks", "thinking"}.isdisjoint(call)
    assert call["max_tokens"] == SETTINGS.max_tokens
    assert response.usage == Usage(120, 40, 3000, 0)
    assert response.text == '{"products": []}'
    assert response.request_id == "req_test"


def test_truncation_and_refusal_are_flagged() -> None:
    assert AnthropicClient(SimpleNamespace(messages=_StubMessages("max_tokens"))).complete(_request()).truncated
    assert AnthropicClient(SimpleNamespace(messages=_StubMessages("refusal"))).complete(_request()).refused


def test_cache_key_covers_model_messages_and_settings() -> None:
    assert _request().key() == _request().key()
    assert _request("a").key() != _request("b").key()
    assert _request(model=SONNET).key() != _request(model=HAIKU).key()


def test_cache_is_write_once_and_reruns_cost_nothing(tmp_path: Path) -> None:
    fake = FakeClient(scripted(['{"products": [1]}', '{"products": [2]}']))
    client = CachedClient(fake, ResponseCache(tmp_path))
    first = client.complete(_request())
    again = client.complete(_request())
    assert first.text == again.text == '{"products": [1]}'
    assert again.from_cache
    assert len(fake.requests) == 1
    assert client.network_calls == 1
    [path] = list(tmp_path.rglob("*.json"))
    before = path.read_bytes()
    ResponseCache(tmp_path).put(_request().key(), first)
    assert path.read_bytes() == before
    assert json.loads(before)["text"] == '{"products": [1]}'


def test_cost() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000, cache_write_tokens=0)
    assert usage.cost_usd(SONNET) == Decimal("2.00") + Decimal("1.00") + Decimal("0.10")
