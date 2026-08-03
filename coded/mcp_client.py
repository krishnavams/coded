"""Optional MCP (Model Context Protocol) stdio client integration.

Runs a dedicated asyncio event loop on a background thread so the rest of the
(synchronous) CLI can call MCP tools with a simple blocking API. If the `mcp`
package is not installed, MCPManager degrades gracefully to a no-op.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Optional, Tuple

try:  # optional dependency
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _HAVE_MCP = True
except Exception:  # noqa: BLE001
    _HAVE_MCP = False


def mcp_available() -> bool:
    return _HAVE_MCP


def _render_content(result: Any) -> str:
    """Turn an MCP CallToolResult into text."""
    parts: List[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(block))
    if getattr(result, "isError", False):
        return "ERROR: " + ("\n".join(parts) or "(unknown MCP error)")
    return "\n".join(parts) if parts else "(no content)"


class MCPManager:
    """Manages the lifecycle of configured MCP stdio servers."""

    def __init__(self, servers: Dict[str, Dict[str, Any]]):
        # Only servers that are not explicitly disabled.
        self.servers = {
            name: cfg for name, cfg in (servers or {}).items() if cfg.get("enabled", True)
        }
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop_event: Optional[asyncio.Event] = None
        self.sessions: Dict[str, Any] = {}
        # (server_name, tool_name, description, input_schema)
        self.tool_specs: List[Tuple[str, str, str, Dict[str, Any]]] = []
        self.errors: List[str] = []

    def start(self, timeout: float = 30.0) -> None:
        if not _HAVE_MCP or not self.servers:
            return
        self.thread = threading.Thread(target=self._run, name="mcp-loop", daemon=True)
        self.thread.start()
        self._ready.wait(timeout=timeout)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"MCP loop crashed: {exc}")
            self._ready.set()

    async def _main(self) -> None:
        from contextlib import AsyncExitStack

        self._stop_event = asyncio.Event()
        async with AsyncExitStack() as stack:
            for name, cfg in self.servers.items():
                try:
                    params = StdioServerParameters(
                        command=cfg["command"],
                        args=cfg.get("args", []),
                        env=cfg.get("env"),
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    listed = await session.list_tools()
                    self.sessions[name] = session
                    for t in listed.tools:
                        self.tool_specs.append(
                            (name, t.name, t.description or "", t.inputSchema or {})
                        )
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"MCP server '{name}' failed to start: {exc}")
            self._ready.set()
            await self._stop_event.wait()

    def call(self, server: str, tool: str, args: Dict[str, Any], timeout: float = 120.0) -> str:
        if not self.loop:
            raise RuntimeError("MCP is not running.")
        fut = asyncio.run_coroutine_threadsafe(self._call(server, tool, args), self.loop)
        return fut.result(timeout=timeout)

    async def _call(self, server: str, tool: str, args: Dict[str, Any]) -> str:
        session = self.sessions.get(server)
        if session is None:
            return f"ERROR: MCP server '{server}' is not connected."
        result = await session.call_tool(tool, args)
        return _render_content(result)

    def build_tools(self, manager_ref: "MCPManager") -> List[Any]:
        from coded.tools.mcp_tool import MCPTool

        return [
            MCPTool(server, tool, desc, schema, manager_ref)
            for (server, tool, desc, schema) in self.tool_specs
        ]

    def stop(self) -> None:
        if self.loop and self._stop_event:
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self.thread:
            self.thread.join(timeout=5)
