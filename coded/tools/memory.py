"""The `memory` tool: keep what this session learned for the next one."""

from __future__ import annotations

from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult


class MemoryTool(Tool):
    name = "memory"
    brief = 'Save a short, durable note about THIS repository for future sessions (architecture, a convention, decision, fix, gotcha, or how to build/test). Not for task state. The user approves each note. actions: "remember" (text, kind), "search" (query), "forget" (id), "list".'
    description = """Remember something about THIS repository that the next session would \
otherwise have to rediscover: how the code is laid out, a convention the code follows, a decision and its reason, a \
gotcha that cost you time, a fact about how to build or test it. Notes are shown to you at \
the start of every future session in this project.

Use it when you learn something durable and non-obvious — not for task state (that is \
todo_write), not for what is already written in the README or AGENTS.md, and not for \
anything a file would say better. One short sentence per note. Saving asks the user for \
approval, so write the note as you would want to read it in a month.

Kinds: `architecture` (which module owns what, how the pieces connect), `convention` (how code here is written), `decision` (what was chosen and why — record \
one when the user settles a design question), `fix` (a bug you fixed whose cause would bite \
again: "symptom → cause → fix", e.g. after the project's checks failed and you fixed them), \
`gotcha`, `fact`. Before working in an area, `search` for earlier notes about it.

actions: "remember" (needs `text`), "search" (needs `query`), "forget" (needs `id`), "list"."""
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["remember", "search", "forget", "list"]},
            "text": {"type": "string", "description": "The note, one sentence."},
            "kind": {"type": "string",
                     "enum": ["architecture", "convention", "decision", "fix", "gotcha", "fact"]},
            "query": {"type": "string", "description": "Words to search the notes for."},
            "id": {"type": "string", "description": "Which note to forget, e.g. 'm3'."},
        },
        "required": ["action"],
    }
    # Writing is gated per call (see run) rather than by the class flag: a note
    # is not a file change, and `requires_permission` would also send the turn
    # through the verification gate.
    requires_permission = False

    def permission_detail(self, args: Dict[str, Any]) -> str:
        return f"Remember for this project: {args.get('text', '')}"

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from coded.memory import KINDS, MemoryStore
        from coded.permissions import Decision

        action = (args.get("action") or "").strip().lower()
        store = MemoryStore.for_project(ctx.cwd)

        if action == "list":
            entries = store.load()
            if not entries:
                return ToolResult.ok("No project memory yet.")
            return ToolResult.ok("\n".join(e.line() for e in entries))

        if action == "search":
            query = (args.get("query") or args.get("text") or "").strip()
            if not query:
                return ToolResult.error("Pass `query`: the words to look for.")
            hits = store.search(query)
            if not hits:
                return ToolResult.ok(f"No project notes about '{query}'.")
            return ToolResult.ok("\n".join(e.line() for e in hits))

        if action == "forget":
            entry_id = (args.get("id") or "").strip()
            if not entry_id:
                return ToolResult.error("Which note? Pass the id from a 'list' call.")
            decision = ctx.permissions.request(
                key="memory", title="Forget a project note",
                detail=f"Remove note {entry_id} from this project's memory.",
                target=f"forget {entry_id}")
            if decision is Decision.DENY:
                return ToolResult.error(ctx.permissions.denial(
                    "forgetting this note", "leave the note in place"))
            return (ToolResult.ok(f"Forgot {entry_id}.") if store.forget(entry_id)
                    else ToolResult.error(f"No note '{entry_id}'. Use action='list' to see them."))

        if action != "remember":
            return ToolResult.error("action must be 'remember', 'search', 'forget' or 'list'.")

        text = " ".join((args.get("text") or "").split())
        if not text:
            return ToolResult.error("Nothing to remember: pass `text`.")
        if len(text) < 10:
            return ToolResult.error("Too short to be useful later. Write a full sentence.")
        kind = args.get("kind") if args.get("kind") in KINDS else "fact"

        # Untrusted content in this turn could be trying to plant a durable
        # instruction, so refuse outright rather than asking.
        if getattr(ctx.permissions, "tainted", False):
            return ToolResult.error(
                "This turn has untrusted content (web, GitHub or MCP output) in context, so "
                "nothing can be written to project memory. Tell the user what you wanted to "
                "record and let them add it with /memory add.")

        decision = ctx.permissions.request(
            key="memory", title="Remember for this project",
            detail=f"({kind}) {text}", target=text)
        if decision is Decision.DENY:
            return ToolResult.error(ctx.permissions.denial(
                "saving this note", "the note was not saved; carry on with the task"))

        entry = store.add(text, kind, source="agent")
        if entry is None:
            return ToolResult.ok("Already known; nothing added.")
        return ToolResult.ok(f"Saved as {entry.id}. It will be in context next session.")
