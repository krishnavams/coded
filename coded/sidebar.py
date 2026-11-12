"""What the TUI's right-hand panel shows, built from agent state.

Kept free of Textual so it can be tested (and reused) without the `tui` extra:
the TUI only places the returned `Text` in a widget.

Sections, each shown only when it has something to say:
  Session   model, mode, context fill, turns/tokens/cost
  Tasks     the todo list with its progress bar (the TUI passes tasks=False:
            it docks the list above the input instead)
  Changed   files written this turn (or last turn), with +/- line counts
  Queued    messages typed while the agent was busy
  Sub-agents  running sub-agents (task tool) with their latest steps, then
            the most recent finished ones
  Shells    background shells and whether they are still running
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from rich.text import Text

from coded import ui

# Wider than this and the panel opens by itself; narrower and it would squeeze
# the transcript, so it stays closed unless asked for (Ctrl+B).
AUTO_OPEN_WIDTH = 110
PANEL_WIDTH = 38
_MAX_DIFF_BYTES = 1_000_000   # bigger files are listed without line counts
_MAX_FILES = 12
_MAX_QUEUED = 5
_MAX_SUBAGENTS = 6   # entries listed (running first, then the newest finished)
_SUBAGENT_STEPS = 2  # latest steps shown under a running one


@dataclass
class FileChange:
    path: str
    added: Optional[int]      # None: too big or unreadable to count
    removed: Optional[int]
    status: str               # "new", "deleted" or "modified"


def file_changes(checkpoints: Any, cwd: str) -> List[FileChange]:
    """Files recorded in the current checkpoint group, compared with disk now.

    `bash` edits are not checkpointed, so they never appear here. Files whose
    content ended up unchanged (written back as they were) are left out.
    """
    group = getattr(checkpoints, "current", None)
    records = getattr(group, "records", None) if group is not None else None
    if not records:
        return []
    out: List[FileChange] = []
    for record in list(records.values()):
        path = Path(record.get("path", ""))
        try:
            rel = str(path.relative_to(Path(cwd).resolve()))
        except (ValueError, OSError):
            rel = str(path)
        existed = bool(record.get("existed"))
        before_path = (group.directory / record["backup"]
                       if existed and record.get("backup") else None)
        exists_now = path.is_file()
        if not existed and not exists_now:
            continue  # created and removed again within the turn
        status = "new" if not existed else "deleted" if not exists_now else "modified"
        before = _lines(before_path) if before_path else []
        after = _lines(path) if exists_now else []
        if before is None or after is None:
            out.append(FileChange(rel, None, None, status))
            continue
        if status == "modified" and before == after:
            continue
        added = removed = 0
        for line in difflib.unified_diff(before, after, n=0, lineterm=""):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        out.append(FileChange(rel, added, removed, status))
    return out


def _lines(path: Optional[Path]) -> Optional[List[str]]:
    """A file's lines, or None when it is too large or not text."""
    if path is None:
        return []
    try:
        if path.stat().st_size > _MAX_DIFF_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def context_fill(agent: Any) -> tuple:
    """(tokens in context now, the window, the compaction point) for the model
    the next request goes to."""
    from coded.compaction import compaction_threshold, current_input_tokens

    model = getattr(agent, "_sized_model", None) or agent.model
    used = current_input_tokens(agent.session)
    return used, model.context_window, compaction_threshold(model, agent.config.compact_ratio)


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _short(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _heading(t: Text, title: str, extra: str = "") -> None:
    if t.plain:
        t.append("\n")
    t.append("◆ ", style=ui.ACCENT)
    t.append(title, style=f"bold {ui.ACCENT}")
    if extra:
        t.append(f"  {extra}", style=ui.ACCENT_SOFT)
    t.append("\n")


def render(agent: Any, *, busy: bool = False, queued: Sequence[str] = (),
           changes: Iterable[FileChange] = (), width: int = PANEL_WIDTH,
           tasks: bool = True) -> Text:
    # The widget spends 3 columns on its border and padding; lines are indented 2.
    inner = max(width - 5, 16)
    t = Text(no_wrap=True, overflow="ellipsis")

    # -- session --------------------------------------------------------------
    _heading(t, "Session", "working…" if busy else "")
    model = getattr(agent, "_sized_model", None) or agent.model
    t.append("  model  ", style=ui.MUTED)
    t.append(_short(model.name, inner - 7) + "\n", style="bold")
    if model is not agent.model:
        t.append(f"         (main: {_short(agent.model.name, inner - 16)})\n", style=ui.MUTED)
    if getattr(agent, "mode", "") == "plan":
        t.append("  mode   ", style=ui.MUTED)
        t.append("plan (read-only)\n", style=f"bold {ui.WARN}")
    used, window, compact_at = context_fill(agent)
    if window:
        share = min(used / window, 1.0)
        cells = 10
        filled = round(share * cells)
        style = ui.WARN if used >= compact_at else ui.ACCENT
        t.append("  ctx    ", style=ui.MUTED)
        t.append("▰" * filled, style=style)
        t.append("▱" * (cells - filled), style=ui.MUTED)
        t.append(f" {share:.0%}\n")
        t.append(f"         {_k(used)} / {_k(window)} tokens\n", style=ui.MUTED)
        t.append(f"         compacts at {_k(compact_at)}\n", style=ui.MUTED)
    s = agent.session
    u = s.total_usage
    tokens = u.prompt_tokens + u.completion_tokens
    t.append(f"  {s.turns} turns · {_k(tokens)} tok · ~${s.estimated_cost(agent.model):.4f}\n",
             style=ui.MUTED)
    if getattr(s, "compactions", 0):
        t.append(f"  compacted {s.compactions}×\n", style=ui.MUTED)

    # -- tasks ----------------------------------------------------------------
    items = list(getattr(agent.todos, "items", []) or []) if tasks else []
    if items:
        done, total, _ = ui.todo_progress(items)
        _heading(t, "Tasks", f"{done}/{total}")
        t.append("  " + ui.todo_bar(items) + "\n")
        marks = {"pending": "☐", "in_progress": "◐", "completed": "☑"}
        for item in items:
            status = item.get("status", "pending")
            style = (f"bold {ui.ACCENT}" if status == "in_progress" else
                     f"{ui.MUTED} strike" if status == "completed" else "")
            t.append("  ")
            t.append(f"{marks.get(status, '☐')} {_short(item.get('content', ''), inner - 2)}",
                     style=style)
            t.append("\n")

    # -- files ----------------------------------------------------------------
    changes = list(changes)
    if changes:
        _heading(t, "Changed this turn" if busy else "Changed last turn", str(len(changes)))
        for change in changes[:_MAX_FILES]:
            counts = Text()
            if change.status == "deleted":
                counts.append("deleted", style=ui.ERR)
            else:
                if change.status == "new":
                    counts.append("new ", style=ui.ACCENT2)
                if change.added is None:
                    counts.append("changed", style=ui.MUTED)
                else:
                    counts.append(f"+{change.added}", style=ui.ACCENT2)
                    if change.status != "new":
                        counts.append(f" −{change.removed}", style=ui.ERR)
            name_width = max(inner - len(counts.plain) - 1, 8)
            name = change.path
            if len(name) > name_width:
                name = "…" + name[-(name_width - 1):]
            t.append(f"  {name:<{name_width}} ")
            t.append_text(counts)
            t.append("\n")
        if len(changes) > _MAX_FILES:
            t.append(f"  … and {len(changes) - _MAX_FILES} more\n", style=ui.MUTED)
        if getattr(agent, "_turn_ran_shell", False):
            t.append("  (bash edits are not listed)\n", style=f"{ui.MUTED} italic")

    # -- queue ----------------------------------------------------------------
    if queued:
        _heading(t, "Queued", str(len(queued)))
        for i, text in enumerate(list(queued)[:_MAX_QUEUED], 1):
            t.append(f"  {i}. {_short(text, inner - 3)}\n")
        if len(queued) > _MAX_QUEUED:
            t.append(f"  … and {len(queued) - _MAX_QUEUED} more\n", style=ui.MUTED)

    # -- sub-agents -----------------------------------------------------------
    manager = getattr(agent, "tasks", None)
    entries = manager.all() if manager is not None else []
    if entries:
        running = [e for e in entries if e.running]
        finished = [e for e in reversed(entries) if not e.running]
        shown = (running + finished)[:_MAX_SUBAGENTS]
        _heading(t, "Sub-agents", f"{len(running)} running" if running else "")
        for e in shown:
            mark, style = (("●", ui.WARN) if e.running else
                           ("✓", ui.ACCENT2) if e.status == "completed" else ("✗", ui.ERR))
            t.append(f"  {mark} ", style=style)
            t.append(f"{e.id} ", style="bold")
            t.append(_short(e.label, max(inner - len(e.id) - 4, 8)) + "\n",
                     style="" if e.running else ui.MUTED)
            where = "background · " if e.background and e.running else ""
            status = "" if e.running or e.status == "completed" else f"{e.status} · "
            t.append(f"    {where}{status}{e.summary()}\n", style=ui.MUTED)
            if e.running:
                for line in list(e.activity)[-_SUBAGENT_STEPS:]:
                    t.append(f"    ⎿ {_short(line, inner - 6)}\n", style=ui.MUTED)
        if len(entries) > len(shown):
            t.append(f"  … and {len(entries) - len(shown)} more\n", style=ui.MUTED)

    # -- shells ---------------------------------------------------------------
    shells = getattr(agent, "shells", None)
    listing = shells.list() if shells is not None else []
    if listing:
        running = sum(1 for _, _, status in listing if status == "running")
        _heading(t, "Shells", f"{running} running")
        for shell_id, command, status in listing:
            style = ui.ACCENT2 if status == "running" else ui.MUTED
            t.append(f"  {shell_id} ", style="bold")
            t.append(f"{status:<9} ", style=style)
            t.append(_short(command, max(inner - len(shell_id) - 11, 8)) + "\n", style=ui.MUTED)

    t.rstrip()
    return t
