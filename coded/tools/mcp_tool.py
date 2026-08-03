"""Wrap a tool exposed by an MCP server as a coded Tool."""

from __future__ import annotations

from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult


class MCPTool(Tool):
    # MCP tool calls can have side effects; gate them behind permission.
    requires_permission = True

    def __init__(self, server_name: str, tool_name: str, description: str,
                 schema: Dict[str, Any], manager: Any):
        # Namespace the tool so multiple servers can't collide.
        self.name = f"mcp__{server_name}__{tool_name}"
        self.description = (description or f"MCP tool {tool_name} from {server_name}.")[:1024]
        self.parameters = schema or {"type": "object", "properties": {}}
        self._server = server_name
        self._tool = tool_name
        self._manager = manager

    def permission_detail(self, args: Dict[str, Any]) -> str:
        return f"MCP {self._server}.{self._tool}({args})"

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            text = self._manager.call(self._server, self._tool, args)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"MCP call failed: {exc}")
        return ToolResult.ok(text)
