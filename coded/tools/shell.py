"""Shell tool: run bash commands with a timeout and permission gate."""

from __future__ import annotations

import subprocess
from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult

_DEFAULT_TIMEOUT = 120
_MAX_OUTPUT = 30_000


class BashTool(Tool):
    name = "bash"
    description = """Run a shell command in the working directory and return its combined \
stdout/stderr and exit code. Use for builds, tests, git status, package managers, etc. \
Prefer the dedicated read/write/edit/grep/glob tools over shell equivalents. Avoid \
interactive commands (they will hang) and destructive commands unless asked."""
    requires_permission = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds (default {_DEFAULT_TIMEOUT}).",
            },
        },
        "required": ["command"],
    }

    def permission_detail(self, args: Dict[str, Any]) -> str:
        return args.get("command", "")

    def permission_target(self, args: Dict[str, Any]) -> str:
        return args.get("command", "")

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"]
        timeout = int(args.get("timeout", _DEFAULT_TIMEOUT))
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=ctx.cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.error(f"Command timed out after {timeout}s: {command}")
        except OSError as exc:
            return ToolResult.error(f"Failed to run command: {exc}")

        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT] + f"\n… (output truncated at {_MAX_OUTPUT} chars)"
        status = f"[exit {proc.returncode}]"
        body = out.strip() if out.strip() else "(no output)"
        result = f"{status}\n{body}"
        return ToolResult(result, is_error=proc.returncode != 0)
