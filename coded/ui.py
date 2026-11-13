"""Terminal rendering helpers built on `rich`."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from rich.box import ROUNDED
from rich.console import Console, Group
from rich.control import Control
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
class TranscriptConsole(Console):
    """A Console that remembers whether the last row it printed was blank, so
    `gap()` can keep exactly one empty line between blocks however many
    `console.print()` calls (notes, panels, prompts) happen in between. A
    Live's redraw (`print(Control())`) is not content and does not count."""

    last_blank = True

    def print(self, *objects, **kwargs):  # noqa: A003 - Console API
        super().print(*objects, **kwargs)
        if objects and all(isinstance(o, Control) for o in objects):
            return
        self.last_blank = all(
            (isinstance(o, str) and not o.strip()) or (isinstance(o, Text) and not o.plain.strip())
            for o in objects)


def make_console(**kwargs) -> TranscriptConsole:
    return TranscriptConsole(theme=_THEME, **kwargs)


console = make_console()
_err_console = Console(stderr=True, theme=_THEME)

# The REPL's transcript layout: assistant text gets a `● ` bullet with a
# hanging indent and its own gap. Off elsewhere (`-p` output stays plain).
_answer_bullets = False


def set_answer_bullets(on: bool) -> None:
    global _answer_bullets
    _answer_bullets = bool(on)


def gap() -> None:
    """One blank line before a new block, never two. A console that does not
    track its last row (a plain Console in a test) always gets the line."""
    if getattr(console, "last_blank", False):
        return
    console.print()


def mark_printed() -> None:
    """Something was drawn outside `console.print` (a Live's final frame)."""
    if hasattr(console, "last_blank"):
        console.last_blank = False


def answer_block(renderable):
    """The assistant's text, as the REPL shows it: `● ` then the text, with
    wrapped lines indented under it."""
    if not _answer_bullets:
        return renderable
    from rich.table import Table

    grid = Table.grid(padding=0, expand=True)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text("● ", style="bold white"), renderable)
    return grid


def answer_gap() -> None:
    if _answer_bullets:
        gap()


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


class _DockStatus:
    """`thinking()` while a TurnDock is up: relabels its spinner, no new Live."""

    def __init__(self, dock: "TurnDock", text: str) -> None:
        self.dock, self.text = dock, text

    def start(self):
        self.dock.set_activity(self.text)
        return self

    def stop(self):
        self.dock.set_activity(None)
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()
        return False


class TurnDock:
    """The bottom of the screen for a whole REPL turn: what the agent is doing,
    the unfinished task list and an input box showing what is being typed.

    One rich Live for the turn (rich allows only one at a time), so
    `thinking()` and the streaming view draw into it instead of opening their
    own; everything printed meanwhile scrolls above it. Without it the keys
    typed mid-turn were echoed straight into the output, where the spinner's
    redraw erased them and no input box was visible at all.
    """

    def __init__(self, get_input, get_tasks=None, get_queued=None, get_running=None,
                 can_detach=None, get_queue=None, get_mode=None) -> None:
        self._get_mode = get_mode or (lambda: "")
        self._get_queue = get_queue or (lambda: [])
        self._get_input = get_input
        self._get_tasks = get_tasks or (lambda: [])
        self._get_queued = get_queued or (lambda: 0)
        self._get_running = get_running or (lambda: [])
        self._can_detach = can_detach or (lambda: False)
        self._activity: Optional[str] = None
        self._body = None
        self._question = False
        self._prompt = None
        self._live = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> "TurnDock":
        global _dock
        if _quiet or self._live is not None:
            return self
        from rich.live import Live

        self._live = Live(get_renderable=self.render, console=console,
                          refresh_per_second=12, transient=True)
        self._live.start()
        _dock = self
        return self

    def stop(self) -> None:
        global _dock
        if _dock is self:
            _dock = None
        live, self._live = self._live, None
        if live is not None:
            try:
                live.stop()
            except Exception:  # noqa: BLE001 - restoring the screen is best effort
                pass

    def __enter__(self) -> "TurnDock":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- content ------------------------------------------------------------
    def set_activity(self, text: Optional[str]) -> None:
        self._activity = text

    def set_body(self, renderable) -> None:
        """Live content above the spinner (the answer as it streams); None clears."""
        self._body = renderable
        if self._live is not None:
            self._live.refresh()

    def asking(self, on: bool) -> None:
        """A prompt is waiting for a typed answer: the box says so."""
        self._question = on

    def set_prompt(self, renderable) -> None:
        """A choice prompt (permission, questions) in place of the spinner and
        input box while it waits for keys; None puts them back."""
        self._prompt = renderable
        if self._live is not None:
            self._live.refresh()

    def render(self):
        from rich.spinner import Spinner

        parts = []
        if self._body is not None:
            parts += [self._body, Text("")]
        if self._prompt is not None:
            return Group(*parts, self._prompt)
        if self._question:
            parts.append(Text.from_markup(f"[{WARN}]● Waiting for your answer…[/]"))
        else:
            if not hasattr(self, "_spinner"):
                self._spinner = Spinner("dots", style=ACCENT)
            self._spinner.update(text=Text.from_markup(
                f"[{ACCENT}]{self._activity or 'Working'}…[/] "
                f"[{MUTED}](esc to interrupt)[/]"))
            parts.append(self._spinner)
        running = list(self._get_running() or [])
        if running:
            parts.insert(0, running_tasks(running, expanded=_expanded))
        dock = todo_dock(list(self._get_tasks() or []))
        if dock is not None:
            parts.append(dock)
        waiting = list(self._get_queue() or [])
        if waiting and not self._question:
            parts.append(queued_list(waiting))
        foreground = any(not t.background for t in running) and self._can_detach()
        parts.append(input_box(self._get_input() or "", self._get_queued() or 0,
                               self._question, foreground_task=foreground,
                               mode=self._get_mode()))
        return Group(*parts)


_dock: Optional[TurnDock] = None


def active_dock() -> Optional[TurnDock]:
    return _dock


_QUEUE_SHOWN = 3


def queued_list(items: list) -> Text:
    """Messages waiting to run after this turn, newest last; ↑ edits the newest."""
    t = Text()
    t.append(f"  ⏎ queued ({len(items)})", style=ACCENT_SOFT)
    t.append("  ↑ to edit", style=MUTED)
    hidden = len(items) - _QUEUE_SHOWN
    if hidden > 0:
        t.append(f"\n     … {hidden} older", style=MUTED)
    for i, item in enumerate(items[-_QUEUE_SHOWN:], start=max(hidden, 0) + 1):
        t.append(f"\n    {i}. ", style=MUTED)
        t.append(_one_line(item, 90))
    return t


def input_box(text: str, queued: int = 0, answering: bool = False,
              foreground_task: bool = False, mode: str = "") -> Group:
    """The input box drawn during a turn (the REPL's own box is not up then)."""
    line = Text()
    line.append("> ", style=f"bold {ACCENT}")
    line.append(text)
    line.append("▌", style=ACCENT)
    if answering:
        hint = "  enter to answer  ·  esc for no"
    else:
        hint = "  enter queues a message  ·  esc to interrupt"
        if foreground_task:
            hint += "  ·  ctrl+b run task in background"
        hint += f"  ·  ctrl+o {'collapse' if _expanded else 'expand'}  ·  ctrl+t tasks"
        if queued:
            hint += "  ·  ↑ edit queued"
    foot = Text(hint, style=MUTED)
    if mode == "accept-edits":
        foot.append("\n  ⏵⏵ accept edits on", style=f"bold {ACCENT2}")
        foot.append("  ·  shift+tab to turn off", style=MUTED)
    elif mode == "plan":
        foot.append("\n  ⏸ plan mode (read-only)", style=f"bold {WARN}")
    elif mode == "default":
        foot.append("\n  shift+tab to accept edits without asking", style=MUTED)
    return Group(Panel(line, box=ROUNDED, border_style=ACCENT_DEEP, padding=(0, 1),
                       expand=True), foot)


def thinking(text: str = "Thinking", tasks: Optional[list] = None):
    """A spinner shown while waiting on the model. No-op in quiet mode.

    With unfinished `tasks`, the task list is docked under the spinner, so it
    stays at the bottom of the screen (next to the input) instead of
    scrolling away with tool output. While a TurnDock is up, its spinner is
    relabelled instead (it already shows the tasks and the input)."""
    if _quiet:
        return _NoStatus()
    if _dock is not None:
        return _DockStatus(_dock, text)
    dock = todo_dock(tasks or [])
    if dock is not None:
        from rich.live import Live
        from rich.spinner import Spinner

        spinner = Spinner("dots", text=Text.from_markup(
            f"[{ACCENT}]{text}…[/] [{MUTED}](Ctrl-C to interrupt)[/]"), style=ACCENT)
        return Live(Group(spinner, dock), console=console, refresh_per_second=12,
                    transient=True)
    return console.status(f"[{ACCENT}]{text}…[/] [{MUTED}](Ctrl-C to interrupt)[/]",
                          spinner="dots", spinner_style=ACCENT)


# Truncation limits for displaying tool activity (the model still sees full output).
_MAX_ARG_LEN = 300
# Folded tool output (as Claude Code shows it): a few lines, then
# "… +N lines (ctrl+o to expand)". Long lines are cut so they cannot wrap
# into many rows. The model always gets the whole result.
_MAX_RESULT_LINES = 3
_MAX_RESULT_CHARS = 600
_MAX_LINE_CHARS = 160


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
    # Full width, like the input box, so the boxes line up at any terminal size.
    return Panel(grid, box=ROUNDED, border_style=ACCENT, padding=(0, 1), expand=True)


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
        answer_gap()
        console.print(answer_block(Markdown(text)))


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
    "multi_edit": ("Edit", "path"), "notebook_edit": ("Edit notebook", "path"),
    "browser": ("Browser", "url"), "ls": ("List", "path"), "delete": ("Delete", "path"),
    "glob": ("Glob", "pattern"), "grep": ("Search", "pattern"),
    "semantic_search": ("Search", "query"), "bash": ("Bash", "command"),
    "bash_output": ("BashOutput", "shell_id"), "kill_shell": ("KillShell", "shell_id"),
    "web_search": ("WebSearch", "query"), "web_fetch": ("Fetch", "url"),
    "task": ("Task", "description"), "skill": ("Skill", "name"),
    "task_output": ("TaskOutput", "task_id"), "task_stop": ("TaskStop", "task_id"),
    "external_agent": ("Agent", "agent"), "apply_patch": ("Patch", None),
    "repo_map": ("Map repo", None),
    "memory": ("Memory", None),
    "todo_write": ("Update tasks", None), "exit_plan_mode": ("Plan ready", None),
    "enter_plan_mode": ("Plan mode", None), "ask_user": ("Question", "question"),
    "view_image": ("View image", "path"),
    "list_mcp_resources": ("MCP resources", "server"),
    "read_mcp_resource": ("Read resource", "uri"),
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
        if name == "ask_user" and not val and isinstance(args.get("questions"), list):
            qs = [q.get("question") for q in args["questions"] if isinstance(q, dict)]
            val = qs[0] if len(qs) == 1 else (f"{len(qs)} questions" if qs else None)
        if val is None or val == "":
            return label, ""
        return label, _rel(val, cwd) if key in _PATH_KEYS else _one_line(val)
    if name == "lsp":
        op = str(args.get("operation") or "")
        what = args.get("symbol") or args.get("query") or ""
        where = _rel(args["path"], cwd) if args.get("path") else ""
        if where and args.get("line"):
            where += f":{args['line']}"
        rest = " in ".join(x for x in (str(what), where) if x)
        return "LSP", _one_line(f"{op} · {rest}" if rest else op, 120)
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
    global _last_call
    _last_call = tool_call_text(name, args, cwd)
    gap()
    console.print(_last_call)


def tool_result_text(content: str, is_error: bool = False, name: str = "",
                     cwd: str = "", max_lines: int = 0, full: bool = False,
                     expand_hint=False) -> Text:
    """Tool output under a `⎿` elbow, shortened for display (the model sees it all).

    `full` shows every line (Ctrl+O); `expand_hint` says how to get there
    (True: the REPL's "(ctrl+o to expand)", or the hint text to use)."""
    max_lines = max_lines or _MAX_RESULT_LINES
    if cwd:
        content = content.replace(cwd.rstrip("/") + "/", "")
    lines = content.splitlines()
    if name == "read" and not is_error and len(lines) > 1 and not full:
        lines = [f"Read {len(lines)} lines"]
    elif name == "todo_write" and not is_error and lines:
        # The list itself is already on screen (panel / dock); keep the
        # headline and any nudge the tool gave the model.
        lines = [lines[0].rstrip(":")] + [ln for ln in lines[1:] if ln.startswith("Note:")]
    if not full:
        if name == "bash" and lines and lines[0] == "[exit 0]":
            lines = lines[1:]  # success is the usual case; a non-zero exit stays
        lines = _squeeze_blank(lines)
    shown = lines if full else [_cut_line(ln) for ln in lines[:max_lines]]
    text = "\n".join(shown)
    cut_chars = not full and len(text) > _MAX_RESULT_CHARS
    if cut_chars:
        text = text[:_MAX_RESULT_CHARS] + "…"
    cut_chars = cut_chars or (not full and any(len(ln) > _line_limit()
                                               for ln in lines[:max_lines]))
    if not text.strip():
        text = "(no output)"
    style = ERR if is_error else MUTED
    body = Text()
    for i, line in enumerate(text.splitlines() or [text]):
        body.append("  ⎿  " if i == 0 else "     ", style=style)
        body.append(line + "\n", style=style)
    extra = len(lines) - len(shown)
    hint = (expand_hint if isinstance(expand_hint, str) else " (ctrl+o to expand)") \
        if expand_hint else ""
    if extra > 0:
        body.append(f"     … +{extra} lines{hint}\n", style=f"{MUTED} italic")
    elif cut_chars and hint:
        body.append(f"     …{hint}\n", style=f"{MUTED} italic")
    return body


def _squeeze_blank(lines: list) -> list:
    """No blank lines at the ends, and at most one in a row (display only)."""
    out: list = []
    for ln in lines:
        if not ln.strip() and (not out or not out[-1].strip()):
            continue
        out.append(ln.rstrip())
    while out and not out[-1].strip():
        out.pop()
    return out


def _line_limit() -> int:
    """Folded lines fit one terminal row (after the 5-column `⎿` indent)."""
    try:
        width = int(console.width) - 6
    except Exception:  # noqa: BLE001
        width = _MAX_LINE_CHARS
    return max(40, min(_MAX_LINE_CHARS, width))


def _cut_line(line: str) -> str:
    limit = _line_limit()
    return line if len(line) <= limit else line[:limit - 1] + "…"


def tool_result(content: str, is_error: bool = False, name: str = "", cwd: str = "") -> None:
    """A tool result in the REPL: shortened (Ctrl+O shows it in full), or whole
    while output is expanded."""
    if _expanded:
        console.print(tool_result_text(content, is_error, name, cwd, full=True), end="")
        return
    text = tool_result_text(content, is_error, name, cwd, expand_hint=True)
    if "ctrl+o to expand" in text.plain:
        _cuts.append((_last_call, content, is_error, name, cwd))
        del _cuts[:-_MAX_CUTS]
    console.print(text, end="")


# Ctrl+O: tool output shown whole instead of shortened (REPL), and the
# running tasks' full activity instead of the last few steps.
_expanded = False
# The results shortened this turn (with their `● Tool(args)` line), newest
# last, so Ctrl+O can show every one of them whole.
_cuts: list = []
_MAX_CUTS = 50
_last_call: Optional[Text] = None


def reset_cuts() -> None:
    """A new turn starts: Ctrl+O expands only what this turn shortened."""
    _cuts.clear()


def is_expanded() -> bool:
    return _expanded


def set_expanded(on: bool) -> None:
    global _expanded
    _expanded = bool(on)


def show_last_cut() -> bool:
    """Print every result shortened this turn in full, each under its tool
    call; False if there is none."""
    if not _cuts:
        return False
    n = len(_cuts)
    console.print(Text(f"  ⊕ full output of {n} shortened result{'s' if n != 1 else ''}:",
                       style=MUTED))
    for call, content, is_error, name, cwd in list(_cuts):
        if call is not None:
            console.print(call)
        console.print(tool_result_text(content, is_error, name, cwd, full=True), end="")
    _cuts.clear()
    return True


# -- tasks (sub-agents) -------------------------------------------------------
_TASK_ACTIVITY = 3  # activity lines per task in the dock, unless expanded


def running_tasks(entries: list, expanded: bool = False) -> Text:
    """Running sub-agents: one headline each, their latest tool calls under it."""
    t = Text()
    for n, e in enumerate(entries):
        if n:
            t.append("\n")
        t.append("● ", style=WARN)
        t.append("Task", style="bold")
        t.append("(", style=MUTED)
        t.append(_one_line(e.label, 60))
        t.append(")", style=MUTED)
        where = "background" if e.background else e.model
        t.append(f"  {e.id} · {where} · {e.summary()}" if where else f"  {e.id} · {e.summary()}",
                 style=MUTED)
        lines = list(e.activity)
        shown = lines if expanded else lines[-_TASK_ACTIVITY:]
        if not shown:
            t.append("\n  ⎿  starting…", style=MUTED)
        hidden = len(lines) - len(shown)
        for i, line in enumerate(shown):
            t.append("\n  ⎿  " if i == 0 else "\n     ", style=MUTED)
            t.append(_one_line(line, 90))
        if hidden:
            t.append(f"\n     … +{hidden} earlier (ctrl+o to expand)", style=f"{MUTED} italic")
    return t


def task_finished_line(e) -> None:
    """Under a foreground Task(...) call once it ends: "⎿ Done (7 tool uses · 1m 2s)"."""
    word = {"completed": "Done", "running": "Done"}.get(e.status, e.status.capitalize())
    console.print(Text(f"  ⎿  {word} ({e.summary()})", style=MUTED))


def task_done(e, pending: bool = False) -> None:
    """A background task has finished (its report goes to the agent)."""
    t = Text()
    t.append("● ", style=ACCENT2 if e.status == "completed" else WARN)
    t.append(f"Background task {e.id} {e.status}", style="bold")
    t.append(f": {_one_line(e.label, 60)} ({e.summary()})", style=MUTED)
    if pending:
        t.append("  — its report goes with your next message", style=MUTED)
    console.print(t)


def task_detail(e) -> None:
    """/tasks <id>: the task's state, every recorded step, and its report."""
    console.print(Text.assemble(("● ", WARN if e.running else ACCENT2),
                                (f"{e.id} ", "bold"), (e.label, "bold"),
                                (f"  {e.status} · {e.model} · {e.summary()}", MUTED)))
    for line in e.activity:
        console.print(Text(f"     {_one_line(line, 110)}", style=MUTED))
    if not e.running:
        console.print(tool_result_text(e.result or "(no output)", max_lines=40), end="")


def tasks_table(entries: list, shells: list) -> None:
    """/tasks: every sub-agent and background shell of this session."""
    from rich.table import Table

    if not entries and not shells:
        info("No tasks yet. The agent starts them with the task tool; "
             "ctrl+b moves a running one to the background.")
        return
    table = Table(box=ROUNDED, border_style=MUTED, title="Tasks", title_justify="left")
    for col in ("id", "kind", "status", "what", "time / tools", "latest"):
        table.add_column(col, style="cyan" if col == "id" else None, overflow="fold")
    for e in entries:
        status = e.status + (" · bg" if e.background and e.running else "")
        table.add_row(e.id, "agent", status, _one_line(e.label, 40), e.summary(),
                      _one_line(e.activity[-1], 40) if e.activity else "")
    for sid, cmd, status in shells:
        table.add_row(sid, "shell", status, _one_line(cmd, 40), "", "")
    console.print(table)
    info("/tasks <id> for details · /tasks stop <id> to stop one")


def info(msg: str) -> None:
    _log().print(f"[{MUTED}]{msg}[/]")


def warn(msg: str) -> None:
    _log().print(f"[{WARN}]![/] {msg}")


def error(msg: str) -> None:
    _log().print(f"[{ERR}]✗[/] {msg}")


def success(msg: str) -> None:
    _log().print(f"[{ACCENT2}]✓[/] {msg}")


def btw_panel(question: str, answer: str) -> Panel:
    """A /btw side answer: framed apart from the work, and not part of it."""
    body = Group(Text(_one_line(question, 200), style=f"bold {ACCENT_SOFT}"), Text(""),
                 Markdown(answer))
    return Panel(body, title=f"[{ACCENT_SOFT}]btw[/]", title_align="left",
                 subtitle=f"[{MUTED}]side answer · not added to the conversation[/]",
                 subtitle_align="left", border_style=MUTED, box=ROUNDED, expand=True)


def question_panel(question: str, options: list) -> None:
    """The agent's question to the user (ask_user), with numbered choices."""
    body = Text(question, style="bold")
    for i, opt in enumerate(options, 1):
        body.append(f"\n  {i}. ", style=ACCENT)
        body.append(opt)
    hint = ("a number or your own answer" if options else "your answer") + " · Enter to skip"
    console.print(Panel(body, title=f"[{ACCENT}]? Question[/]", title_align="left",
                        subtitle=f"[{MUTED}]{hint}[/]", subtitle_align="left",
                        border_style=ACCENT, box=ROUNDED, expand=True))


def pick_answer(answer: Optional[str], options: list) -> Optional[str]:
    """A typed answer to a question: a choice's number becomes the choice."""
    if answer is None:
        return None
    answer = answer.strip()
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    return answer


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
            body.append("     ")  # the indent stays unshaded, under the tool call
            body.append(f"{new:>4} + {line[1:]}", style="#86efac on #12301f")
            body.append("\n")
            new += 1
        elif line.startswith("-"):
            body.append("     ")
            body.append(f"{old:>4} - {line[1:]}", style="#fca5a5 on #3a1620")
            body.append("\n")
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


_DOCK_ITEMS = 6


def todo_dock(items: list, max_items: int = _DOCK_ITEMS) -> Optional[Text]:
    """A compact task list for the bottom of the screen during a turn.

    None when there is nothing unfinished. Long lists show a window around
    the current task, with a count of what is hidden."""
    done, total, _ = todo_progress(items)
    if not total or done == total:
        return None
    current = next((n for n, i in enumerate(items) if i.get("status") == "in_progress"),
                   next((n for n, i in enumerate(items) if i.get("status") != "completed"), 0))
    start = max(0, min(current - 1, total - max_items))
    shown = items[start:start + max_items]
    t = Text()
    t.append(f"  {todo_title(items)}", style=ACCENT_SOFT)
    if start:
        t.append(f"\n    … {start} more above", style=MUTED)
    for item in shown:
        status = item.get("status", "pending")
        mark, color = _TODO_MARK.get(status, ("☐", MUTED))
        style = f"{MUTED} strike" if status == "completed" else (
            "bold" if status == "in_progress" else "")
        t.append(f"\n    {mark} ", style=color)
        t.append(_one_line(item.get("content", ""), 90), style=style)
    rest = total - start - len(shown)
    if rest > 0:
        t.append(f"\n    … {rest} more", style=MUTED)
    return t


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
    t.rstrip()
    console.print(Panel(t, title=todo_title(items), title_align="left", border_style=MUTED,
                        box=ROUNDED, expand=True))
