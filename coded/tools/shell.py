"""Shell tools: bash (optionally sandboxed / backgrounded), bash_output, kill_shell."""

from __future__ import annotations

import re
import subprocess
from typing import Any, Dict

from coded.limits import get_limits
from coded.sandbox import sandbox_state, wrap_command
from coded.shells import active_shell, run as run_command
from coded.tools.base import Tool, ToolContext, ToolResult

_DEFAULT_TIMEOUT = 120
_MAX_OUTPUT = 30_000


def _truncate(out: str, limit: int = _MAX_OUTPUT) -> str:
    if len(out) > limit:
        return out[:limit] + f"\n… (output truncated at {limit} chars)"
    return out


class BashTool(Tool):
    name = "bash"
    brief = 'Run a shell command; returns output and exit code. Prefer read/edit/grep/glob over shell equivalents. Never run interactive commands. For servers or long jobs set run_in_background=true, then use bash_output and kill_shell.'
    description = """Run a shell command in the working directory and return its combined \
stdout/stderr and exit code. Use for builds, tests, git status, package managers, etc. \
Prefer the dedicated read/write/edit/grep/glob tools over shell equivalents. Avoid \
interactive commands (they will hang) and destructive commands unless asked. For servers, \
watchers or long jobs set run_in_background=true, then poll with bash_output and stop \
with kill_shell. When a sandbox is active, writes outside the working directory and \
network access fail; set dangerously_disable_sandbox=true only if the command truly needs \
them (the user will be asked)."""
    requires_permission = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds (default {_DEFAULT_TIMEOUT}).",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Start the command in the background and return a shell id.",
            },
            "dangerously_disable_sandbox": {
                "type": "boolean",
                "description": "Run outside the sandbox (requires user approval).",
            },
        },
        "required": ["command"],
    }

    def permission_detail(self, args: Dict[str, Any]) -> str:
        cmd = args.get("command", "")
        if args.get("run_in_background"):
            cmd += "   (background)"
        return cmd

    def permission_target(self, args: Dict[str, Any]) -> str:
        return args.get("command", "")

    def _sandboxed(self, args: Dict[str, Any], ctx: ToolContext):
        settings, backend = sandbox_state(ctx.config)
        if not settings.enabled or args.get("dangerously_disable_sandbox"):
            return settings, None
        return settings, backend

    def auto_allowed(self, args: Dict[str, Any], ctx: ToolContext) -> bool:
        settings, backend = self._sandboxed(args, ctx)
        return bool(backend) and settings.auto_allow

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"]
        settings, backend = self._sandboxed(args, ctx)
        argv: Any = command
        note = ""
        if settings.enabled and not args.get("dangerously_disable_sandbox"):
            if backend:
                argv = wrap_command(command, ctx.cwd, settings, backend)
                note = f" [sandboxed: {backend}{', no network' if not settings.network else ''}]"
            elif settings.fail_if_unavailable:
                return ToolResult.error(
                    "Sandbox is enabled but no backend (bwrap on Linux, sandbox-exec on "
                    "macOS; Windows has none) is installed; refusing to run unsandboxed.")

        if args.get("run_in_background"):
            shells = ctx.extra.get("shells")
            if shells is None:
                return ToolResult.error("Background shells are not available here.")
            try:
                shell_id = shells.start(argv, ctx.cwd, command)
            except OSError as exc:
                return ToolResult.error(f"Failed to start: {exc}")
            return ToolResult.ok(
                f"Started background shell {shell_id}{note}: {command}\n"
                f"Check output with bash_output(shell_id='{shell_id}'); "
                f"stop with kill_shell(shell_id='{shell_id}').")

        timeout = int(args.get("timeout", _DEFAULT_TIMEOUT))
        try:
            proc = run_command(argv, cwd=ctx.cwd, timeout=timeout,
                               shell=active_shell(ctx.config))
        except subprocess.TimeoutExpired:
            hint = "For long-running processes use run_in_background=true."
            if re.match(r"\s*(grep|egrep|rg|ag|find|ack)\b", command):
                hint = ("A search over the whole tree is slow here (.venv, node_modules, "
                        "data): use the grep/glob tools, which skip those, or narrow the "
                        "path and add --exclude-dir.")
            return ToolResult.error(
                f"Command timed out after {timeout}s (it was stopped): {command}. {hint}")
        except OSError as exc:
            return ToolResult.error(f"Failed to run command: {exc}")

        out = _truncate((proc.stdout or "") + (proc.stderr or ""), get_limits(ctx).shell_output_chars)
        status = f"[exit {proc.returncode}]{note}"
        body = out.strip() if out.strip() else "(no output)"
        return ToolResult(f"{status}\n{body}", is_error=proc.returncode != 0)


class BashOutputTool(Tool):
    name = "bash_output"
    brief = 'Read new output from a background shell and whether it is still running. Optional `filter` regex.'
    description = """Read new output from a background shell started with \
bash(run_in_background=true), plus whether it is still running. Only output produced since \
the last read is returned. Optional `filter` keeps only lines matching a regex."""
    parallel_safe = True
    parameters = {
        "type": "object",
        "properties": {
            "shell_id": {"type": "string"},
            "filter": {"type": "string", "description": "Regex; only matching lines are returned."},
        },
        "required": ["shell_id"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        shells = ctx.extra.get("shells")
        got = shells.read(args["shell_id"]) if shells else None
        if got is None:
            return ToolResult.error(f"No background shell '{args['shell_id']}'.")
        output, status = got
        if args.get("filter"):
            try:
                rx = re.compile(args["filter"])
            except re.error as exc:
                return ToolResult.error(f"Invalid filter regex: {exc}")
            output = "\n".join(ln for ln in output.splitlines() if rx.search(ln))
        body = _truncate(output, get_limits(ctx).shell_output_chars).strip() or "(no new output)"
        return ToolResult.ok(f"[{status}]\n{body}")


class KillShellTool(Tool):
    name = "kill_shell"
    description = "Stop a background shell started with bash(run_in_background=true)."
    parameters = {
        "type": "object",
        "properties": {"shell_id": {"type": "string"}},
        "required": ["shell_id"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        shells = ctx.extra.get("shells")
        if not shells or not shells.kill(args["shell_id"]):
            return ToolResult.error(f"No background shell '{args['shell_id']}'.")
        return ToolResult.ok(f"Stopped {args['shell_id']}.")
