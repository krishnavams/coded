"""Expose coded as an MCP server (``coded mcp-serve``), so other agents and
MCP clients (Claude Code, Claude Desktop, Cursor, …) can delegate tasks to it.

Tools offered:
  - ``coded_task(prompt, cwd?, model?)``: run a full coded agent turn and return
    its final answer. Risky actions are denied unless the server was started
    with ``--yolo`` (there is nobody to ask) or allowed by permission rules.
  - ``coded_review(target?, cwd?)``: run the reviewer → qa → security pipeline.

Needs the optional ``mcp`` package: ``pip install 'coded[mcp]'``.
"""

from __future__ import annotations

import argparse
import os
from typing import List, Optional


def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP


def build_server(args: argparse.Namespace):
    from coded import ui
    from coded.agent import create_agent
    from coded.config import load_config
    from coded.permissions import PermissionManager
    from coded.skills import discover_skills
    from coded.telemetry import tracer_from_config

    default_cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    server = _server_class()("coded", instructions=(
        "coded is a coding agent for OpenAI-compatible models. Delegate self-contained "
        "coding tasks to coded_task; give complete, standalone instructions."))

    def _agent(cwd: Optional[str], model: Optional[str]):
        cwd = os.path.abspath(cwd) if cwd else default_cwd
        cfg = load_config(cwd=cwd, config_path=args.config)
        mc = cfg.get_model(model or args.model)
        perms = PermissionManager(auto_approve=args.yolo, rules=cfg.permissions,
                                  injection_guard=cfg.injection_guard)
        return create_agent(model=mc, config=cfg, cwd=cwd, permissions=perms,
                            skills=discover_skills(cwd), stream=False, verbose=False,
                            tracer=tracer_from_config(cfg))

    @server.tool()
    def coded_task(prompt: str, cwd: Optional[str] = None, model: Optional[str] = None) -> str:
        """Run a coding task with the coded agent and return its final answer.

        prompt: complete, standalone instruction. cwd: project directory (defaults
        to the server's). model: configured model alias (optional)."""
        agent = _agent(cwd, model)
        result = agent.run(prompt)
        return result or "(no answer)"

    @server.tool()
    def coded_review(target: Optional[str] = None, cwd: Optional[str] = None) -> str:
        """Review code changes (default: uncommitted git changes) with coded's
        reviewer → qa → security pipeline and return the report."""
        from coded.review import run_review

        return run_review(_agent(cwd, None), target, verbose=False)

    ui.set_quiet(True)
    return server


def main(argv: Optional[List[str]] = None) -> int:
    from coded import ui
    from coded.mcp_client import mcp_available

    p = argparse.ArgumentParser(prog="coded mcp-serve", description="Run coded as an MCP server (stdio).")
    p.add_argument("-m", "--model", help="Default model alias.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Default project directory.")
    p.add_argument("--yolo", action="store_true",
                   help="Auto-approve tool actions (otherwise risky actions are denied).")
    args = p.parse_args(argv)
    if not mcp_available():
        ui.error("coded mcp-serve needs the 'mcp' package: pip install 'coded[mcp]'")
        return 1
    build_server(args).run()
    return 0
