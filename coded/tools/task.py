"""Task tool: spawn a focused sub-agent for a self-contained piece of work."""

from __future__ import annotations

from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult


class TaskTool(Tool):
    name = "task"
    description = """Delegate a self-contained task to a sub-agent that has the same file \
and shell tools and its own separate context. Use this for large searches or multi-step \
subtasks so your own context stays focused. Give a complete, standalone instruction — the \
sub-agent cannot ask follow-up questions. It returns a final text report."""
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short (3-6 word) label for the task."},
            "prompt": {
                "type": "string",
                "description": "The full, self-contained instruction for the sub-agent.",
            },
        },
        "required": ["prompt"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.spawn_subagent is None:
            return ToolResult.error("Sub-agents are not available in this context.")
        prompt = args["prompt"]
        try:
            result = ctx.spawn_subagent(prompt)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the model
            return ToolResult.error(f"Sub-agent failed: {exc}")
        return ToolResult.ok(result or "(sub-agent returned no output)")
