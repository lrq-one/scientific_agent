from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import ROOT
from app.models.schemas import ToolResult


class ScientificMCPClient:
    """Real MCP stdio client; the server runs as a separate Python process."""

    def __init__(self, cwd: Path = ROOT):
        self.parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server"],
            cwd=cwd,
        )

    async def list_tools(self) -> list[str]:
        async with stdio_client(self.parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [tool.name for tool in result.tools]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        async with stdio_client(self.parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                available = {tool.name for tool in (await session.list_tools()).tools}
                if name not in available:
                    return ToolResult(success=False, source=f"mcp:{name}", error="MCP tool not available")
                response = await session.call_tool(name, arguments)
                data: Any = response.structuredContent
                if not data and response.content:
                    text = getattr(response.content[0], "text", "")
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        data = {"text": text}
                return ToolResult(
                    success=not response.isError,
                    data=data,
                    source=f"mcp:{name}",
                    metadata={"transport": "stdio", "server": "app.mcp_server", "listed_tools": sorted(available)},
                    error="MCP server returned an error" if response.isError else None,
                )

