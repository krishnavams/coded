"""Task tool: spawn a focused sub-agent for a self-contained piece of work."""

from __future__ import annotations

import threading
from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult


class TaskTool(Tool):
    # A sub-agent edits files with the parent's permissions, so a turn that
    # delegated has changed things and must be verified like any other.
    mutates = True
    name = "task"
    brief = 'Delegate a self-contained task to a sub-agent with its own context. Give a complete instruction: it cannot ask questions. Returns a report. isolation="worktree" keeps its edits on a separate branch; run_in_background=true returns at once and its report arrives later.'
    description = """Delegate a self-contained task to a sub-agent that has the same file \
and shell tools and its own separate context. Use this for large searches or multi-step \
subtasks so your own context stays focused. Give a complete, standalone instruction — the \
sub-agent cannot ask follow-up questions. It returns a final text report.

Set isolation="worktree" to run it in a separate git worktree on its own branch, so its \
edits can't collide with yours or with other sub-agents; several isolated tasks requested \
together may run in parallel. The report says where the changes were left.

Set run_in_background=true for long, independent work (e.g. reviewing one area while \
you do another): it returns at once with a task id, and the report arrives as a message \
when it finishes. A background task cannot ask the user for approval."""
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
            "run_in_background": {
                "type": "boolean",
                "description": "Start it and return at once; its report arrives as a "
                               "message when it finishes. For independent, long work.",
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
        label = args.get("description") or "task"
        tasks = ctx.extra.get("tasks")
        if tasks is None:  # no task manager (a bare context): run inline as before
            text, is_error = _work(args, ctx, None)
            return ToolResult(text, is_error)
        if args.get("run_in_background"):
            entry = tasks.start(label, background=True)
            _start_thread(args, ctx, tasks, entry)
            return ToolResult.ok(
                f"Started background task {entry.id} ({label}). Its report will arrive as a "
                "message when it finishes; meanwhile keep working or end your turn. Use "
                f"task_output {entry.id} to check on it, task_stop to cancel it.")
        entry = tasks.start(label)
        if not tasks.can_detach:
            text, is_error = _work(args, ctx, entry)
            tasks.finish(entry, text, "failed" if is_error else "completed")
            return ToolResult(text, is_error)
        # Run on a worker so the user can move it to the background (Ctrl+B).
        _start_thread(args, ctx, tasks, entry)
        try:
            while not entry.done.wait(0.1):
                if entry.detach.is_set():
                    entry.background = True
                    if entry.done.is_set():
                        break
                    return ToolResult.ok(
                        f"The user moved this task to the background as {entry.id} "
                        f"({label}). Its report will arrive as a message when it finishes; "
                        "continue with other work or end your turn.")
        except KeyboardInterrupt:
            tasks.stop(entry.id)
            raise
        if entry.background:  # finished just as it was detached: deliver it here
            entry.delivered = True
        return ToolResult(entry.result, entry.status == "failed")


def _work(args: Dict[str, Any], ctx: ToolContext, entry) -> "tuple[str, bool]":
    """Run the sub-agent (in a worktree if asked); (report, is_error)."""
    prompt = args["prompt"]
    label = args.get("description") or "task"
    kw: Dict[str, Any] = {"model": args["model"]} if args.get("model") else {}
    if entry is not None:
        kw["task"] = entry
    if args.get("isolation") == "worktree":
        from coded.worktree import WorktreeError, create_worktree, finalize_worktree

        try:
            path, branch, root, base = create_worktree(ctx.cwd, label)
        except WorktreeError as exc:
            return f"Could not create worktree: {exc}", True
        try:
            result = ctx.spawn_subagent(prompt, cwd=path, **kw)
        except Exception as exc:  # noqa: BLE001
            result = f"Sub-agent failed: {exc}"
        try:
            where = finalize_worktree(path, branch, root, base)
        except Exception as exc:  # noqa: BLE001
            where = f"(could not inspect worktree {path}: {exc})"
        return f"{result or '(sub-agent returned no output)'}\n\n[worktree] {where}", False
    try:
        result = ctx.spawn_subagent(prompt, **kw)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the model
        return f"Sub-agent failed: {exc}", True
    return result or "(sub-agent returned no output)", False


def _start_thread(args: Dict[str, Any], ctx: ToolContext, tasks, entry) -> None:
    def target() -> None:
        # Never prompt once in the background: nobody is watching this task.
        # Nor once stopped (Esc): its thread may still be finishing a step.
        ctx.permissions.set_background(lambda: entry.background or not entry.running)
        try:
            text, is_error = _work(args, ctx, entry)
        except BaseException as exc:  # noqa: BLE001 - the task ends either way
            text, is_error = f"Sub-agent failed: {exc}", True
        tasks.finish(entry, text, "failed" if is_error else "completed")

    threading.Thread(target=target, name=f"coded-{entry.id}", daemon=True).start()


class TaskOutputTool(Tool):
    name = "task_output"
    parallel_safe = True
    brief = "Check a background task: its status, recent activity, and its report once done."
    description = """Check on a background task started with task(run_in_background=true): \
its status, tool uses, running time and latest activity, and its report once it has \
finished. Without task_id, lists every task."""
    parameters = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "e.g. t2"}},
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        tasks = ctx.extra.get("tasks")
        if tasks is None:
            return ToolResult.error("No tasks in this context.")
        task_id = args.get("task_id")
        if not task_id:
            rows = [f"{t.id}  {t.status:<9} {t.label}  ({t.summary()})" for t in tasks.all()]
            return ToolResult.ok("\n".join(rows) or "No tasks.")
        t = tasks.get(task_id)
        if t is None:
            return ToolResult.error(f"No task '{task_id}'. Call task_output without an id "
                                    "to list them.")
        lines = [f"{t.id} \"{t.label}\": {t.status} — {t.summary()}"]
        if t.running:
            lines += ["Recent activity:"] + [f"  {a}" for a in list(t.activity)[-8:]]
        else:
            t.delivered = True
            lines += ["Report:", t.result or "(no output)"]
        return ToolResult.ok("\n".join(lines))


class TaskStopTool(Tool):
    name = "task_stop"
    brief = "Stop a running background task."
    description = "Stop a running background task (it ends after its current step)."
    parameters = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "e.g. t2"}},
        "required": ["task_id"],
    }

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        tasks = ctx.extra.get("tasks")
        if tasks is None or not tasks.stop(args["task_id"]):
            return ToolResult.error(f"No running task '{args['task_id']}'.")
        return ToolResult.ok(f"Stopping {args['task_id']}; it ends after its current step.")
