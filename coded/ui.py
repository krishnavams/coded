"""Terminal rendering helpers built on `rich`."""

from __future__ import annotations

import json
import os
from typing import Any

from rich.box import ROUNDED
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

_quiet = False

# --- palette (works on dark and light terminals) ---------------------------
ACCENT = "#a78bfa"     # primary accent (purple)
ACCENT_DEEP = "#7c3aed"  # darker purple (logo shading, selections)
ACCENT_SOFT = "#c4b5fd"  # light purple (inline code, highlights)
ACCENT2 = "#5ad1a0"    # success/green
MUTED = "#8a94a7"      # dimmed text
WARN = "#e0af68"       # amber
ERR = "#f7768e"        # red

_THEME = Theme({
    "markdown.code": f"bold {ACCENT_SOFT}",
    "markdown.h1": f"bold {ACCENT}", "markdown.h2": f"bold {ACCENT}",
    "markdown.h3": f"bold {ACCENT_SOFT}", "markdown.link": ACCENT,
    "markdown.link_url": ACCENT, "markdown.item.bullet": ACCENT,
    "markdown.item.number": ACCENT, "markdown.block_quote": MUTED,
})
console = Console(theme=_THEME)
_err_console = Console(stderr=True, theme=_THEME)


def set_quiet(enabled: bool) -> None:
    """In quiet mode, log helpers write to stderr so stdout stays clean (JSON)."""
    global _quiet
    _quiet = enabled


def _log() -> Console:
    return _err_console if _quiet else console


class _NoStatus:
    def start(self):
        return self

    def stop(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def thinking(text: str = "Thinking"):
    """A spinner shown while waiting on the model. No-op in quiet mode."""
    if _quiet:
        return _NoStatus()
    return console.status(f"[{ACCENT}]{text}…[/] [{MUTED}](Ctrl-C to interrupt)[/]",
                          spinner="dots", spinner_style=ACCENT)


# Truncation limits for displaying tool activity (the model still sees full output).
_MAX_ARG_LEN = 300
_MAX_RESULT_LINES = 12
_MAX_RESULT_CHARS = 1600


def _short_path(cwd: str, width: int = 48) -> str:
    if len(cwd) <= width:
        return cwd
    return "…" + cwd[-(width - 1):]


# coded's own mark: a pair of code brackets around a diamond.
LOGO = (" ▟▀▀ ▀▀▙ ",
        " █  ◆  █ ",
        " ▜▄▄ ▄▄▛ ")


def logo_text() -> Text:
    t = Text()
    for i, row in enumerate(LOGO):
        for ch in row:
            t.append(ch, style=f"bold {ACCENT_SOFT}" if ch == "◆" else ACCENT)
        if i < len(LOGO) - 1:
            t.append("\n")
    return t


def welcome(model_name: str, cwd: str) -> Panel:
    """The start-of-session card (shared by the REPL and the TUI)."""
    from rich.table import Table

    from coded import __version__

    right = Text()
    right.append("coded", style=f"bold {ACCENT}")
    right.append(f"  v{__version__}\n", style=MUTED)
    right.append(model_name, style="bold")
    right.append("  ·  ", style=MUTED)
    right.append(_short_path(_home_short(cwd)) + "\n", style=MUTED)
    right.append("/help", style=ACCENT)
    right.append(" for commands  ·  ", style=MUTED)
    right.append("/model", style=ACCENT)
    right.append(" to switch  ·  ", style=MUTED)
    right.append("Ctrl-C", style=ACCENT)
    right.append(" to interrupt", style=MUTED)
    grid = Table.grid(padding=(0, 2))
    grid.add_row(logo_text(), right)
    return Panel(grid, box=ROUNDED, border_style=ACCENT, padding=(0, 1), expand=False)


def banner(model_name: str, cwd: str) -> None:
    console.print(welcome(model_name, cwd))


def user_prefix() -> None:
    console.print()


def echo_user(text: str) -> None:
    """Echo an auto-submitted user message (e.g. the initial prompt)."""
    line = Text()
    line.append("> ", style=f"bold {ACCENT}")
    line.append(text, style="bold")
    console.print(line)


def assistant_markdown(text: str) -> None:
    if text.strip():
        console.print(Markdown(text))


def _home_short(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if home != "/" and (path + "/").startswith(home + "/") else path


def _rel(path: Any, cwd: str = "") -> str:
    """A path as the user thinks of it: relative to the project when inside it,
    always with forward slashes (shorter, and what the model wrote)."""
    path = str(path)
    if cwd and os.path.isabs(path):
        try:
            rel = os.path.relpath(path, cwd)
        except ValueError:
            return path.replace("\\", "/")
        if not rel.startswith(".."):
            return rel.replace("\\", "/")
    return _home_short(path).replace("\\", "/")


def _one_line(value: Any, width: int = 100) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= width else text[:width - 1] + "…"


# How each tool call is titled: display name + which argument is the headline.
_TOOL_TITLES = {
    "read": ("Read", "path"), "write": ("Write", "path"), "edit": ("Edit", "path"),
    "multi_edit": ("Edit", "path"), "ls": ("List", "path"), "delete": ("Delete", "path"),
    "glob": ("Glob", "pattern"), "grep": ("Search", "pattern"),
    "semantic_search": ("Search", "query"), "bash": ("Bash", "command"),
    "bash_output": ("BashOutput", "shell_id"), "kill_shell": ("KillShell", "shell_id"),
    "web_search": ("WebSearch", "query"), "web_fetch": ("Fetch", "url"),
    "task": ("Task", "description"), "skill": ("Skill", "name"),
    "external_agent": ("Agent", "agent"), "apply_patch": ("Patch", None),
    "todo_write": ("Update tasks", None), "exit_plan_mode": ("Plan ready", None),
}
_PATH_KEYS = {"path", "source", "destination", "cwd"}


def tool_title(name: str, args: dict, cwd: str = "") -> tuple:
    """(display name, argument summary) for a tool call, e.g. ("Edit", "app.py")."""
    args = args if isinstance(args, dict) else {}
    if name in _TOOL_TITLES:
        label, key = _TOOL_TITLES[name]
        val = args.get(key) if key else None
        if name == "task" and not val:
            val = args.get("prompt")
        if name == "git":
            val = args.get("command") or args.get("args")
        if val is None or val == "":
            return label, ""
        return label, _rel(val, cwd) if key in _PATH_KEYS else _one_line(val)
    if name == "move":
        return "Move", f"{_rel(args.get('source', ''), cwd)} → {_rel(args.get('destination', ''), cwd)}"
    label = name if name.startswith("mcp") else name[:1].upper() + name[1:]
    parts = []
    for k, v in args.items():
        v = _rel(v, cwd) if k in _PATH_KEYS else (v if isinstance(v, str) else _short_args(v))
        parts.append(_one_line(v, 60) if len(args) == 1 else f"{k}: {_one_line(v, 40)}")
    return label, _one_line(", ".join(parts), 120)


def _short_args(args: Any) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(args)
    if len(s) > _MAX_ARG_LEN:
        s = s[:_MAX_ARG_LEN] + "…"
    return s


def tool_call_text(name: str, args: dict, cwd: str = "") -> Text:
    label, summary = tool_title(name, args, cwd)
    t = Text()
    t.append("● ", style=ACCENT)
    t.append(label, style="bold")
    if summary:
        t.append("(", style=MUTED)
        t.append(summary)
        t.append(")", style=MUTED)
    return t


def tool_call(name: str, args: dict, cwd: str = "") -> None:
    console.print()
    console.print(tool_call_text(name, args, cwd))


def tool_result_text(content: str, is_error: bool = False, name: str = "",
                     cwd: str = "", max_lines: int = 0) -> Text:
    """Tool output under a `⎿` elbow, shortened for display (the model sees it all)."""
    max_lines = max_lines or _MAX_RESULT_LINES
    if cwd:
        content = content.replace(cwd.rstrip("/") + "/", "")
    lines = content.splitlines()
    if name == "read" and not is_error and len(lines) > 1:
        lines = [f"Read {len(lines)} lines"]
    shown = lines[:max_lines]
    text = "\n".join(shown)
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "…"
    if not text.strip():
        text = "(no output)"
    style = ERR if is_error else MUTED
    body = Text()
    for i, line in enumerate(text.splitlines() or [text]):
        body.append("  ⎿  " if i == 0 else "     ", style=style)
        body.append(line + "\n", style=style)
    extra = len(lines) - len(shown)
    if extra > 0:
        body.append(f"     … +{extra} lines\n", style=f"{MUTED} italic")
    return body


def tool_result(content: str, is_error: bool = False, name: str = "", cwd: str = "") -> None:
    console.print(tool_result_text(content, is_error, name, cwd), end="")


def info(msg: str) -> None:
    _log().print(f"[{MUTED}]{msg}[/]")


def warn(msg: str) -> None:
    _log().print(f"[{WARN}]![/] {msg}")


def error(msg: str) -> None:
    _log().print(f"[{ERR}]✗[/] {msg}")


def success(msg: str) -> None:
    _log().print(f"[{ACCENT2}]✓[/] {msg}")


def permission_panel(title: str, detail: str) -> None:
    """A framed prompt describing an action awaiting approval."""
    body = Text()
    for line in (detail.splitlines() or [detail]):
        body.append(line + "\n", style="white")
    console.print(Panel(body, title=f"[{WARN}]{title}[/]", title_align="left",
                        box=ROUNDED, border_style=WARN, padding=(0, 1), expand=False))


def rule(msg: str = "") -> None:
    _log().rule(f"[dim]{msg}[/dim]" if msg else "")


_MAX_DIFF_LINES = 60


def diff_text(text: str, max_lines: int = _MAX_DIFF_LINES) -> Text:
    """A unified diff as colored lines with old/new line numbers."""
    import re

    body = Text()
    old = new = 0
    shown = 0
    lines = [ln for ln in text.splitlines() if not ln.startswith(("--- ", "+++ "))]
    for line in lines:
        if shown >= max_lines:
            break
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
        if m:
            if shown:
                body.append("       ⋯\n", style=MUTED)
            old, new = int(m.group(1)), int(m.group(2))
            continue
        if line.startswith("+"):
            body.append(f"     {new:>4} + {line[1:]}\n", style="#86efac on #12301f")
            new += 1
        elif line.startswith("-"):
            body.append(f"     {old:>4} - {line[1:]}\n", style="#fca5a5 on #3a1620")
            old += 1
        else:
            body.append(f"     {new:>4}   {line[1:]}\n", style=MUTED)
            old += 1
            new += 1
        shown += 1
    if len(lines) > max_lines:
        body.append(f"     … +{len(lines) - max_lines} more diff lines\n", style=f"{MUTED} italic")
    return body


def diff(text: str) -> None:
    """Render a unified diff with line numbers and +/- coloring."""
    if text.strip():
        console.print(diff_text(text), end="")


_TODO_MARK = {"pending": ("☐", MUTED), "in_progress": ("◐", WARN), "completed": ("☑", ACCENT2)}


def todo_progress(items: list) -> tuple:
    """(done, total, percent) for a task list."""
    total = len(items)
    done = sum(1 for i in items if i.get("status") == "completed")
    return done, total, int(round(100 * done / total)) if total else 0


def todo_bar(items: list, width: int = 12) -> str:
    """A text progress bar: filled for done, a half block for work in progress."""
    done, total, _ = todo_progress(items)
    if not total:
        return ""
    active = any(i.get("status") == "in_progress" for i in items)
    filled = int(width * done / total)
    bar = "▰" * filled
    if active and filled < width:
        bar += "▨"  # the running item, distinct from both done and pending
    return bar.ljust(width, "▱")


def todo_title(items: list) -> str:
    """"Tasks 2/5 ▰▰▰▱▱ 40%" — the same summary everywhere it is shown."""
    done, total, pct = todo_progress(items)
    if not total:
        return "Tasks"
    return f"Tasks  {done}/{total}  {todo_bar(items)}  {pct}%"


def current_todo(items: list) -> str:
    """What the agent says it is doing right now, for a status line."""
    for item in items:
        if item.get("status") == "in_progress":
            return item.get("content", "")
    return ""


def todos(items: list) -> None:
    """Render the agent's task list (todo_write)."""
    if _quiet:
        return
    t = Text()
    for i, item in enumerate(items, 1):
        status = item.get("status", "pending")
        mark, color = _TODO_MARK.get(status, ("☐", MUTED))
        style = f"{MUTED} strike" if status == "completed" else (
            "bold" if status == "in_progress" else "")
        t.append(f"  {mark} ", style=color)
        t.append(item.get("content", ""), style=style)
        if status == "in_progress":
            t.append("   ← now", style=WARN)
        t.append("\n")
    console.print(Panel(t, title=todo_title(items), title_align="left", border_style=MUTED,
                        box=ROUNDED, expand=False))
