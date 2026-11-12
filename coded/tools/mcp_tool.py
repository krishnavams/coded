"""Wrap a tool exposed by an MCP server as a coded Tool."""

from __future__ import annotations

from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult


class MCPTool(Tool):
    # MCP tool calls can have side effects; gate them behind permission.
    requires_permission = True
    untrusted_output = True

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


class ListMcpResourcesTool(Tool):
    name = "list_mcp_resources"
    parallel_safe = True
    untrusted_output = True
    brief = "List the resources (files, docs, records) your MCP servers offer."
    description = """List the resources the connected MCP servers offer (files, documents, database records, …) with their URIs. Read one with read_mcp_resource."""
    parameters = {
        "type": "object",
        "properties": {"server": {"type": "string",
                                  "description": "Only this server (default: all)."}},
    }

    def __init__(self, manager: Any):
        self._manager = manager

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult.ok(self._manager.list_resources(args.get("server") or None))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"Listing MCP resources failed: {exc}")


class ReadMcpResourceTool(Tool):
    name = "read_mcp_resource"
    parallel_safe = True
    untrusted_output = True
    brief = "Read one MCP resource by server and URI."
    description = """Read one resource from an MCP server, by the server name and the URI that list_mcp_resources showed."""
    parameters = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "The MCP server's name."},
            "uri": {"type": "string", "description": "The resource URI."},
        },
        "required": ["server", "uri"],
    }

    def __init__(self, manager: Any):
        self._manager = manager

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        server, uri = str(args.get("server") or ""), str(args.get("uri") or "")
        if not server or not uri:
            return ToolResult.error("Give both `server` and `uri` (see list_mcp_resources).")
        try:
            return ToolResult.ok(self._manager.read_resource(server, uri))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"Reading {uri} from {server} failed: {exc}")
