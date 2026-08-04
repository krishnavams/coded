"""Dedicated git tool: read-only subcommands run freely; mutating ones are gated."""

from __future__ import annotations

import shlex
import subprocess
from typing import Any, Dict

from coded.permissions import Decision
from coded.tools.base import Tool, ToolContext, ToolResult

# Subcommands that only read repository state — safe to run without prompting.
_READ_ONLY = {
    "status", "diff", "log", "show", "blame", "branch", "remote", "tag",
    "rev-parse", "ls-files", "shortlog", "describe", "reflog", "cat-file",
    "config",  # reading config; writes still require --global/set which we gate below
}
_MAX_OUTPUT = 30_000


class GitTool(Tool):
    name = "git"
    description = """Run a git command in the repository. Read-only subcommands \
(status, diff, log, show, blame, branch, remote, ...) run without approval. \
Mutating subcommands (commit, checkout, reset, push, merge, add, ...) ask first. \
Pass the arguments after `git`, e.g. "status", "diff HEAD~1", "log --oneline -10"."""
    # Permission is decided per-subcommand inside run().
    requires_permission = False
    parameters = {
        "type": "object",
        "properties": {
            "args": {"type": "string", "description": "Arguments after `git`, e.g. 'log --oneline -5'."},
        },
        "required": ["args"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = args["args"].strip()
        try:
            tokens = shlex.split(raw)
        except ValueError as exc:
            return ToolResult.error(f"Could not parse git args: {exc}")
        if not tokens:
            return ToolResult.error("No git subcommand given.")

        sub = tokens[0]
        # Treat `config` with a value assignment as a write.
        is_write_config = sub == "config" and any(
            not t.startswith("-") for t in tokens[2:]
        )
        read_only = sub in _READ_ONLY and not is_write_config

        if not read_only:
            decision = ctx.permissions.request(
                key=f"git:{sub}", title="Run git command", detail=f"git {raw}"
            )
            if decision is Decision.DENY:
                return ToolResult.error("The user denied this git command. Do not retry it.")

        try:
            proc = subprocess.run(
                ["git", *tokens], cwd=ctx.cwd, capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError:
            return ToolResult.error("git is not installed or not on PATH.")
        except subprocess.TimeoutExpired:
            return ToolResult.error("git command timed out.")

        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT] + "\n… (truncated)"
        body = out.strip() or "(no output)"
        return ToolResult(f"[git {sub} exit {proc.returncode}]\n{body}", is_error=proc.returncode != 0)
