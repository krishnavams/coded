"""Optional MCP (Model Context Protocol) stdio client integration.

Runs a dedicated asyncio event loop on a background thread so the rest of the
(synchronous) CLI can call MCP tools with a simple blocking API. If the `mcp`
package is not installed, MCPManager degrades gracefully to a no-op.
"""

from __future__ import annotations

import asyncio
import os
import re
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


_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def expand_refs(value: str, env: Optional[Dict[str, str]] = None) -> str:
    """`${VAR}` / `${VAR:-default}` → the environment value (other agents'
    configs reference secrets this way). An unset VAR with no default is ''."""
    env = os.environ if env is None else env
    return _REF.sub(lambda m: env.get(m.group(1), m.group(2) or ""), str(value))


def unset_refs(cfg: Dict[str, Any], env: Optional[Dict[str, str]] = None) -> List[str]:
    """Names a server's `${VAR}` references (without a default) that are not set."""
    env = os.environ if env is None else env
    values = [cfg.get("command", ""), *cfg.get("args", []), *(cfg.get("env") or {}).values()]
    names = [m.group(1) for v in values for m in _REF.finditer(str(v)) if m.group(2) is None]
    return sorted({n for n in names if n not in env})


def server_params(cfg: Dict[str, Any], env: Optional[Dict[str, str]] = None
                  ) -> Tuple[str, List[str], Optional[Dict[str, str]]]:
    """(command, args, env) for a configured server, with `${VAR}` expanded."""
    env = dict(os.environ if env is None else env)
    extra = {k: expand_refs(v, env) for k, v in (cfg.get("env") or {}).items()}
    # Server-specific vars on top of our environment (keeps PATH etc.).
    return (expand_refs(cfg["command"], env),
            [expand_refs(a, env) for a in cfg.get("args", [])],
            {**env, **extra} if extra else None)


def _render_content(result: Any) -> str:
    """Turn an MCP CallToolResult into text."""
    parts: List[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(block))
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
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
        self.resource_servers: List[str] = []  # servers that offer resources

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
                missing = unset_refs(cfg)
                if missing:
                    self.errors.append(
                        f"MCP server '{name}' uses ${{{'}, ${'.join(missing)}}}, which "
                        f"{'is' if len(missing) == 1 else 'are'} not set in your environment "
                        "(used as empty).")
                try:
                    command, args, env = server_params(cfg)
                    params = StdioServerParameters(command=command, args=args, env=env)
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    listed = await session.list_tools()
                    self.sessions[name] = session
                    try:  # servers without the resources capability refuse this
                        if (await session.list_resources()).resources:
                            self.resource_servers.append(name)
                    except Exception:  # noqa: BLE001
                        pass
                    for t in listed.tools:
                        self.tool_specs.append(
                            (name, t.name, t.description or "",
                             # mcp 1.x: inputSchema; mcp 2.x: input_schema
                             getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {})
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

    def list_resources(self, server: Optional[str] = None, timeout: float = 60.0) -> str:
        if not self.loop:
            raise RuntimeError("MCP is not running.")
        names = [server] if server else list(self.resource_servers)
        lines: List[str] = []
        for name in names:
            session = self.sessions.get(name)
            if session is None:
                lines.append(f"[{name}] not connected")
                continue
            fut = asyncio.run_coroutine_threadsafe(session.list_resources(), self.loop)
            for r in fut.result(timeout=timeout).resources:
                mime = getattr(r, "mime_type", None) or getattr(r, "mimeType", None)
                desc = f" — {r.description}" if getattr(r, "description", None) else ""
                lines.append(f"[{name}] {r.uri}  {r.name}{f' ({mime})' if mime else ''}{desc}")
        return "\n".join(lines) or "No resources."

    def read_resource(self, server: str, uri: str, timeout: float = 60.0) -> str:
        if not self.loop:
            raise RuntimeError("MCP is not running.")
        session = self.sessions.get(server)
        if session is None:
            return f"ERROR: MCP server '{server}' is not connected."
        fut = asyncio.run_coroutine_threadsafe(session.read_resource(uri), self.loop)
        result = fut.result(timeout=timeout)
        parts: List[str] = []
        for c in getattr(result, "contents", []) or []:
            if getattr(c, "text", None) is not None:
                parts.append(c.text)
            else:
                mime = getattr(c, "mime_type", None) or getattr(c, "mimeType", None) or "binary"
                parts.append(f"({mime} content, {len(getattr(c, 'blob', '') or '')} base64 "
                             "chars; not shown)")
        return "\n".join(parts) or "(empty resource)"

    def build_tools(self, manager_ref: "MCPManager") -> List[Any]:
        from coded.tools.mcp_tool import ListMcpResourcesTool, MCPTool, ReadMcpResourceTool

        tools: List[Any] = [
            MCPTool(server, tool, desc, schema, manager_ref)
            for (server, tool, desc, schema) in self.tool_specs
        ]
        if self.resource_servers:  # only then: their definitions go out with every request
            tools += [ListMcpResourcesTool(manager_ref), ReadMcpResourceTool(manager_ref)]
        return tools

    def stop(self) -> None:
        if self.loop and self._stop_event:
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self.thread:
            self.thread.join(timeout=5)
