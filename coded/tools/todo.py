"""Task tracking (todo_write) and plan mode (exit_plan_mode)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from coded.tools.base import Tool, ToolContext, ToolResult

STATUSES = ("pending", "in_progress", "completed")
_MARK = {"pending": "☐", "in_progress": "◐", "completed": "☑"}


@dataclass
class TodoStore:
    items: List[Dict[str, str]] = field(default_factory=list)
    on_change: Optional[Callable[[List[Dict[str, str]]], None]] = None

    def set(self, items: List[Dict[str, str]]) -> None:
        self.items = items
        if self.on_change:
            self.on_change(items)

    def render(self) -> str:
        if not self.items:
            return "(no tasks)"
        return "\n".join(f"{_MARK.get(t['status'], '☐')} {t['content']}" for t in self.items)


class TodoWriteTool(Tool):
    name = "todo_write"
    brief = 'Task list for work with 3+ steps: write the whole list, keep one item in_progress, mark items completed as soon as they are done. Each call replaces the list.'
    description = """Create and update a structured task list for the current work. Use it \
for any task with 3+ steps or when the user gives several requests: write the full list \
up front, keep exactly one item `in_progress`, and mark items `completed` as soon as they \
are done (don't batch). Each call replaces the whole list. Skip it for trivial one-step \
tasks."""
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The complete, updated task list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Imperative task, e.g. 'Add tests'."},
                        "status": {"type": "string", "enum": list(STATUSES)},
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        store: Optional[TodoStore] = ctx.extra.get("todos")
        if store is None:
            return ToolResult.error("Task tracking is not available in this context.")
        raw = args.get("todos")
        if not isinstance(raw, list):
            return ToolResult.error("`todos` must be an array of {content, status}.")
        items = []
        for i, t in enumerate(raw):
            if not isinstance(t, dict) or not str(t.get("content", "")).strip():
                return ToolResult.error(f"todos[{i}] needs a non-empty `content`.")
            status = str(t.get("status", "pending"))
            if status not in STATUSES:
                return ToolResult.error(f"todos[{i}].status must be one of {', '.join(STATUSES)}.")
            items.append({"content": str(t["content"]).strip(), "status": status})
        store.set(items)
        active = sum(1 for t in items if t["status"] == "in_progress")
        note = ""
        if active > 1:
            note = "\nNote: keep only one task in_progress at a time."
        elif active == 0 and any(t["status"] == "pending" for t in items):
            note = "\nNote: mark the task you start next as in_progress."
        return ToolResult.ok("Task list updated:\n" + store.render() + note)


PLAN_MODE_REMINDER = (
    "[Plan mode is ON] Research only: you may read, search and run read-only tools, but "
    "you must NOT edit files, run shell commands, or make any other changes. When you "
    "have a complete plan, call exit_plan_mode with it for the user's approval."
)


class ExitPlanModeTool(Tool):
    name = "exit_plan_mode"
    brief = 'Plan mode only: present your finished plan for approval. If approved, implement it.'
    description = """Use only in plan mode: present your finished implementation plan \
(Markdown, concise, step by step) for the user's approval. If approved, plan mode ends and \
you should implement it; if rejected, revise the plan as the user asks."""
    parameters = {
        "type": "object",
        "properties": {"plan": {"type": "string", "description": "The plan, in Markdown."}},
        "required": ["plan"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        get_mode = ctx.extra.get("get_mode")
        set_mode = ctx.extra.get("set_mode")
        if get_mode is None or set_mode is None or get_mode() != "plan":
            return ToolResult.error("Not in plan mode; just proceed with the work.")
        plan = str(args.get("plan", "")).strip()
        if not plan:
            return ToolResult.error("Provide the plan text.")
        answer = ctx.permissions.ask("Approve this plan and start implementing?", plan)
        if answer is None:
            return ToolResult.ok(
                "Plan recorded. This session is non-interactive, so plan mode stays on; "
                "report the plan as your final answer.")
        if answer.strip().lower() in ("y", "yes", "a", "always"):
            set_mode("default")
            return ToolResult.ok(
                "The user approved the plan. Plan mode is off — implement it now, "
                "tracking the steps with todo_write.")
        return ToolResult.ok(
            "The user rejected the plan. Stay in plan mode and ask what to change, "
            "or revise the plan.")


class EnterPlanModeTool(Tool):
    name = "enter_plan_mode"
    brief = "Switch to plan mode (read-only research, then exit_plan_mode with a plan)."
    description = """Switch to plan mode before a large or risky change whose approach you should agree with the user first: research read-only (edits and commands are refused), then present the plan with exit_plan_mode. Not for small, clear tasks — just do those."""
    parameters = {"type": "object", "properties": {}}

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        get_mode = ctx.extra.get("get_mode")
        set_mode = ctx.extra.get("set_mode")
        if get_mode is None or set_mode is None:
            return ToolResult.error("Plan mode is not available here.")
        if get_mode() == "plan":
            return ToolResult.ok("Already in plan mode. Present the plan with exit_plan_mode.")
        set_mode("plan")
        return ToolResult.ok(
            "Plan mode is on: research read-only (edits and commands are refused), then "
            "present your plan with exit_plan_mode for the user's approval.")
