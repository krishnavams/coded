"""The Agent Command Center: one view of what this session is doing and costing.

`/dashboard` in the REPL (a snapshot; also works mid-turn) and the TUI (a
modal that refreshes while open). Drawn from state coded already keeps — the
session, its event log (`events.py`), the task manager, background shells and
the todo list — by one plain render function with no Textual import, so both
front-ends show the same thing and it is testable alone.
"""

from __future__ import annotations

import time
from typing import Any, List

from rich.box import ROUNDED
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from coded import ui
from coded.events import model_usage, recent_problems, tool_stats


def _secs(ms: float) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{int(ms)}ms"


def _ago(t: float) -> str:
    from coded.tasks import format_elapsed

    return format_elapsed(max(0, time.time() - t)) + " ago"


def _table(title: str, columns: List[str]) -> Table:
    # Only tables with a free-text column fill the width; number tables fit.
    wide = any(c in ("what", "latest", "problem") for c in columns)
    table = Table(box=ROUNDED, border_style=ui.ACCENT_DEEP, title=title, title_justify="left",
                  title_style=f"bold {ui.ACCENT}", expand=wide, pad_edge=False)
    text_cols = ("what", "latest", "problem", "model", "tool", "id", "status", "when")
    for i, col in enumerate(columns):
        wide = col in ("what", "latest", "problem")
        table.add_column(col, justify="left" if col in text_cols else "right",
                         overflow="ellipsis", no_wrap=True,
                         ratio=1 if wide else None, min_width=None if wide else len(col))
    return table


def render(agent: Any, *, busy: bool = False, width: int = 100) -> Group:
    events = agent.events.snapshot() if hasattr(agent, "events") else []
    session = agent.session
    parts: List[Any] = []

    # -- overview -------------------------------------------------------------
    u = session.total_usage
    cost = session.estimated_cost(agent.model)
    started = next((e["t"] for e in events if "t" in e), None)
    head = Text()
    head.append("◆ ", style=ui.ACCENT)
    head.append(agent.model.name, style="bold")
    head.append(f"  ·  {agent.permission_mode}", style=ui.MUTED)
    head.append(f"  ·  {'working' if busy else 'idle'}", style=ui.WARN if busy else ui.ACCENT2)
    requests = sum(1 for e in events if e.get("kind") == "turn")
    head.append(f"\n{requests} request{'s' if requests != 1 else ''} ({session.turns} model "
                f"calls)  ·  {u.prompt_tokens:,} in / {u.completion_tokens:,} out"
                f"{f' ({u.cached_tokens:,} cached)' if u.cached_tokens else ''}  ·  ",
                style=ui.MUTED)
    head.append(f"${cost:.4f}", style="bold")
    budget = getattr(agent.config, "max_cost", None)
    if budget:
        head.append(f" of ${budget:.2f} budget", style=ui.MUTED)
    if started:
        head.append(f"  ·  started {_ago(started)}", style=ui.MUTED)
    calls = sum(1 for e in events if e.get("kind") == "tool")
    errors = sum(1 for e in events if e.get("kind") == "tool" and e.get("error"))
    head.append(f"\n{calls} tool calls  ·  ", style=ui.MUTED)
    head.append(f"{errors} failed or denied", style=ui.ERR if errors else ui.MUTED)
    parts.append(Panel(head, title=f"[bold {ui.ACCENT}]Command Center[/]", title_align="left",
                       border_style=ui.ACCENT, box=ROUNDED))

    # -- agents -----------------------------------------------------------------
    agents = _table("Agents", ["id", "status", "what", "model", "time / tools", "latest"])
    agents.add_row("main", "working" if busy else "idle", ui._one_line(
        getattr(agent, "_last_request", "") or "—", 40), agent.model.name, "", "")
    for t in agent.tasks.all():
        status = t.status + (" · bg" if t.background and t.running else "")
        agents.add_row(t.id, status, ui._one_line(t.label, 40), t.model or "", t.summary(),
                       ui._one_line(t.activity[-1], 36) if t.activity else "")
    shells = agent.shells.list() if getattr(agent, "shells", None) else []
    for sid, cmd, status in shells:
        agents.add_row(sid, status, ui._one_line(cmd, 40), "shell", "", "")
    parts.append(agents)

    # -- models -----------------------------------------------------------------
    usage = model_usage(events)
    if usage:
        models = _table("Models", ["model", "calls", "input", "output", "cost"])
        for name, m in usage.items():
            models.add_row(name, str(int(m["calls"])), f"{int(m['input']):,}",
                           f"{int(m['output']):,}", f"${m['cost']:.4f}")
        parts.append(models)

    # -- tools ------------------------------------------------------------------
    stats = tool_stats(events)
    if stats:
        tools = _table("Tools", ["tool", "calls", "failed", "denied", "avg", "total"])
        for name, s in list(stats.items())[:12]:
            n = int(s["calls"])
            tools.add_row(name, str(n),
                          Text(str(int(s["errors"])), style=ui.ERR if s["errors"] else ui.MUTED),
                          Text(str(int(s["denied"])), style=ui.WARN if s["denied"] else ui.MUTED),
                          _secs(s["ms"] / max(n, 1)), _secs(s["ms"]))
        parts.append(tools)

    # -- progress and checks ----------------------------------------------------
    todos = list(getattr(agent.todos, "items", []) or [])
    status = Text()
    if todos:
        done, total, _ = ui.todo_progress(todos)
        status.append("tasks  ", style=ui.MUTED)
        status.append(f"{ui.todo_bar(todos, 16)} {done}/{total}")
        current = ui.current_todo(todos)
        if current:
            status.append(f"  ·  now: {ui._one_line(current, 50)}", style=ui.MUTED)
        status.append("\n")
    from coded.verify import VerifySettings

    vs = VerifySettings.from_config(agent.config.verify)
    heal = getattr(agent, "self_heal", False) or vs.auto
    status.append("checks ", style=ui.MUTED)
    status.append((vs.command or ("auto (/heal)" if heal else "off")))
    last = next((e for e in reversed(events) if e.get("kind") == "verify"), None)
    if last:
        colour = {"pass": ui.ACCENT2, "fail": ui.ERR, "stall": ui.WARN}.get(last["result"], ui.MUTED)
        status.append("  ·  last: ", style=ui.MUTED)
        status.append(last["result"], style=colour)
        status.append(f" {_ago(last['t'])}", style=ui.MUTED)
    parts.append(Panel(status, title=f"[bold {ui.ACCENT}]Progress[/]", title_align="left",
                       border_style=ui.ACCENT_DEEP, box=ROUNDED))

    # -- problems -----------------------------------------------------------------
    problems = recent_problems(events)
    if problems:
        table = _table("Recent problems", ["when", "what", "problem"])
        for e in reversed(problems):
            kind = e["kind"]
            if kind == "tool":
                what = e.get("name", "?") + (f" ({e['task']})" if e.get("task") else "")
                root = agent.cwd.rstrip("/\\")
                detail = (e.get("detail") or "failed").replace(root + "/", "").replace(
                    root + "\\", "")
                if e.get("denied"):
                    said = detail.split("said what to do instead:", 1)
                    who = {"user": "denied by you", "rule": "refused by a deny rule",
                           "plan": "not run: plan mode", "hook": "blocked by a hook",
                           "unattended": "refused: nobody to ask"}.get(e.get("reason"), "denied")
                    why = who + (f": {said[1].strip()}" if len(said) > 1 else "")
                else:
                    why = detail
            elif kind == "verify":
                what, why = "checks", f"{e['result']}: {e.get('detail') or e.get('command', '')}"
            else:
                what, why = kind.replace("_", " "), e.get("limit", "") and f"limit {e['limit']}"
            table.add_row(_ago(e["t"]), what, ui._one_line(str(why), 70))
        parts.append(table)
    return Group(*parts)
