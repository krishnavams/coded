"""Task tool: spawn a focused sub-agent for a self-contained piece of work."""

from __future__ import annotations

from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult


class TaskTool(Tool):
    # A sub-agent edits files with the parent's permissions, so a turn that
    # delegated has changed things and must be verified like any other.
    mutates = True
    name = "task"
    brief = 'Delegate a self-contained task to a sub-agent with its own context. Give a complete instruction: it cannot ask questions. Returns a report. isolation="worktree" keeps its edits on a separate branch.'
    description = """Delegate a self-contained task to a sub-agent that has the same file \
and shell tools and its own separate context. Use this for large searches or multi-step \
subtasks so your own context stays focused. Give a complete, standalone instruction — the \
sub-agent cannot ask follow-up questions. It returns a final text report.

Set isolation="worktree" to run it in a separate git worktree on its own branch, so its \
edits can't collide with yours or with other sub-agents; several isolated tasks requested \
together may run in parallel. The report says where the changes were left."""
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short (3-6 word) label for the task."},
            "prompt": {
                "type": "string",
                "description": "The full, self-contained instruction for the sub-agent.",
            },
            "isolation": {
                "type": "string",
                "enum": ["none", "worktree"],
                "description": "Run in the shared working tree (default) or an isolated git worktree.",
            },
            "model": {
                "type": "string",
                "description": "Optional configured model alias for the sub-agent "
                               "(e.g. a cheaper model for searches). Default: the subagent role model.",
            },
        },
        "required": ["prompt"],
    }

    def is_parallel_safe(self, args: Dict[str, Any], ctx: ToolContext) -> bool:
        return args.get("isolation") == "worktree" and ctx.permissions.can_prompt_concurrently()

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.spawn_subagent is None:
            return ToolResult.error("Sub-agents are not available in this context.")
        prompt = args["prompt"]
        label = args.get("description") or "task"
        kw = {"model": args["model"]} if args.get("model") else {}
        if args.get("isolation") == "worktree":
            from coded.worktree import WorktreeError, create_worktree, finalize_worktree

            try:
                path, branch, root, base = create_worktree(ctx.cwd, label)
            except WorktreeError as exc:
                return ToolResult.error(f"Could not create worktree: {exc}")
            try:
                result = ctx.spawn_subagent(prompt, cwd=path, **kw)
            except Exception as exc:  # noqa: BLE001
                result = f"Sub-agent failed: {exc}"
            try:
                where = finalize_worktree(path, branch, root, base)
            except Exception as exc:  # noqa: BLE001
                where = f"(could not inspect worktree {path}: {exc})"
            return ToolResult.ok(f"{result or '(sub-agent returned no output)'}\n\n[worktree] {where}")
        try:
            result = ctx.spawn_subagent(prompt, **kw)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the model
            return ToolResult.error(f"Sub-agent failed: {exc}")
        return ToolResult.ok(result or "(sub-agent returned no output)")
