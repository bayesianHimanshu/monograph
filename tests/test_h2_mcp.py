"""The MCP adapter lists the registry's tools and calls one (in-process, with a stub store)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import anyio
from mcp import Client

from monograph.core.meta import DEFAULT_BASE
from monograph.tools.mcp_server import build_server
from monograph.tools.registry import TOOLS
from monograph.tools.service import ToolService

PRODUCT = {"iri": f"{DEFAULT_BASE}product/0069-1000", "product_ndc": "0069-1000", "name": "Brandx"}


class StubBackend:
    name = "stub"

    def by_property(self, label: str, prop: str, values: Sequence[str]) -> list[dict[str, Any]]:
        return [PRODUCT] if label == "Product" and prop == "product_ndc" and "0069-1000" in values else []

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: []


def test_lists_every_tool_and_calls_one() -> None:
    server = build_server(ToolService(StubBackend()))

    async def go() -> tuple[list[str], dict[str, Any], bool]:
        async with Client(server) as client:
            listed = await client.list_tools()
            result = await client.call_tool("resolve_entity", {"text": "0069-1000", "kind": "product"})
            bad = await client.call_tool("get_entity", {"ids": []})
            text = result.content[0].text  # type: ignore[union-attr]
            return [t.name for t in listed.tools], json.loads(text), bool(bad.is_error)

    names, payload, bad_is_error = anyio.run(go)
    assert names == [t.name for t in TOOLS]
    assert payload["candidates"] == [
        {"id": PRODUCT["iri"], "kind": "product", "display": "Brandx", "match": "product_ndc"}
    ]
    assert bad_is_error
