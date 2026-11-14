"""Dedicated git tool: read-only subcommands run freely; mutating ones are gated."""

from __future__ import annotations

import shlex
import subprocess

from coded.shells import text_kwargs
from typing import Any, Dict

from coded.permissions import Decision
from coded.shells import IS_WINDOWS
from coded.limits import get_limits
from coded.tools.base import Tool, ToolContext, ToolResult

# Subcommands that only read repository state — safe to run without prompting.
_READ_ONLY = {
    "status", "diff", "log", "show", "blame", "branch", "remote", "tag",
    "rev-parse", "ls-files", "shortlog", "describe", "reflog", "cat-file",
    "config",  # reading config; writes still require --global/set which we gate below
}
_MAX_OUTPUT = 30_000


def split_args(raw: str) -> list:
    """Split a git argument string. On Windows, backslashes are path separators,
    not escapes, so POSIX splitting would turn `src\app.py` into `srcapp.py`."""
    if not IS_WINDOWS:
        return shlex.split(raw)
    tokens = shlex.split(raw, posix=False)
    return [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]


class GitTool(Tool):
    name = "git"
    brief = 'Run git with the arguments after `git`, e.g. "diff HEAD~1". Read-only commands run freely; commit, checkout, push and other changes ask first.'
    description = """Run a git command in the repository. Read-only subcommands \
(status, diff, log, show, blame, branch, remote, ...) run without approval. \
Mutating subcommands (commit, checkout, reset, push, merge, add, ...) ask first. \
Pass the arguments after `git`, e.g. "status", "diff HEAD~1", "log --oneline -10"."""
    # Permission is decided per-subcommand inside run().
    requires_permission = False  # read-only subcommands are free; see run()
    mutates = True  # a push/commit changes things, so the turn gets verified
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
            tokens = split_args(raw)
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
                key="git", title="Run git command", detail=f"git {raw}", target=raw,
            )
            if decision is Decision.DENY:
                return ToolResult.error(ctx.permissions.denial("this git command"))

        try:
            proc = subprocess.run(
                ["git", *tokens], cwd=ctx.cwd, capture_output=True, timeout=60,
                **text_kwargs(),
            )
        except FileNotFoundError:
            return ToolResult.error("git is not installed or not on PATH.")
        except subprocess.TimeoutExpired:
            return ToolResult.error("git command timed out.")

        out = (proc.stdout or "") + (proc.stderr or "")
        limit = get_limits(ctx).git_output_chars
        if len(out) > limit:
            out = out[:limit] + "\n… (truncated)"
        body = out.strip() or "(no output)"
        return ToolResult(f"[git {sub} exit {proc.returncode}]\n{body}", is_error=proc.returncode != 0)
