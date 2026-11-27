"""A thin MCP adapter (stdio, official `mcp` SDK) over the same tool registry and service.

The experiment calls tools in-process for determinism; this adapter exists so the same tools can be used
from any MCP client. Run: `monograph h2 mcp --store sql|graph`.
"""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from pydantic import ValidationError

from monograph.tools.registry import TOOLS, ToolError, tool_schema
from monograph.tools.service import ToolService


def build_server(service: ToolService) -> Server[Any]:
    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[types.Tool(name=t.name, description=t.description, input_schema=tool_schema(t)) for t in TOOLS]
        )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            out = service.call(params.name, params.arguments or {})
        except (ToolError, ValidationError) as exc:
            return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc))], is_error=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=out.model_dump_json())])

    return Server(
        "monograph-h2",
        version="1",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve_stdio(service: ToolService) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(service)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
