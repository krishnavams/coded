"""Terminal rendering helpers built on `rich`."""

from __future__ import annotations

import json
from typing import Any

from rich.box import ROUNDED
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

console = Console()
_err_console = Console(stderr=True)
_quiet = False

# --- palette (works on dark and light terminals) ---------------------------
ACCENT = "#7c9cff"     # primary accent (violet-blue)
ACCENT2 = "#5ad1a0"    # success/green
MUTED = "#8a94a7"      # dimmed text
WARN = "#e0af68"       # amber
ERR = "#f7768e"        # red


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
    return console.status(f"[{MUTED}]{text}…[/]", spinner="dots", spinner_style=ACCENT)


# Truncation limits for displaying tool activity (the model still sees full output).
_MAX_ARG_LEN = 300
_MAX_RESULT_LINES = 12
_MAX_RESULT_CHARS = 1600


def _short_path(cwd: str, width: int = 48) -> str:
    if len(cwd) <= width:
        return cwd
    return "…" + cwd[-(width - 1):]


def banner(model_name: str, cwd: str) -> None:
    from coded import __version__

    title = Text()
    title.append("◆ ", style=ACCENT)
    title.append("coded ", style=f"bold {ACCENT}")
    title.append(f"v{__version__}", style=MUTED)
    title.append("   a coding agent for OpenAI-compatible models", style=MUTED)

    meta = Text()
    meta.append("  model ", style=MUTED)
    meta.append(model_name, style=f"bold {ACCENT2}")
    meta.append("     dir ", style=MUTED)
    meta.append(_short_path(cwd), style="white")

    hint = Text()
    hint.append("  /help", style=ACCENT)
    hint.append(" commands   ", style=MUTED)
    hint.append("/model", style=ACCENT)
    hint.append(" switch   ", style=MUTED)
    hint.append("Ctrl-C", style=ACCENT)
    hint.append(" interrupt   ", style=MUTED)
    hint.append("/exit", style=ACCENT)
    hint.append(" quit", style=MUTED)

    panel = Panel(
        Group(title, Text(""), meta, Text(""), hint),
        box=ROUNDED, border_style=ACCENT, padding=(0, 1), expand=False,
    )
    console.print(panel)


def user_prefix() -> None:
    console.print()


def echo_user(text: str) -> None:
    """Echo an auto-submitted user message (e.g. the initial prompt)."""
    line = Text()
    line.append("❯ ", style=f"bold {ACCENT}")
    line.append(text, style="white")
    console.print(line)


def assistant_markdown(text: str) -> None:
    if text.strip():
        console.print(Markdown(text))


def _short_args(args: dict) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(args)
    if len(s) > _MAX_ARG_LEN:
        s = s[:_MAX_ARG_LEN] + "…"
    return s


def tool_call(name: str, args: dict) -> None:
    label = Text()
    label.append("● ", style=ACCENT)
    label.append(name, style=f"bold {ACCENT}")
    label.append("  ")
    label.append(_short_args(args), style=MUTED)
    console.print(label)


def tool_result(content: str, is_error: bool = False) -> None:
    lines = content.splitlines()
    shown = lines[:_MAX_RESULT_LINES]
    text = "\n".join(shown)
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "…"
    extra = len(lines) - len(shown)
    style = ERR if is_error else MUTED
    gutter_style = ERR if is_error else ACCENT
    if not text.strip():
        text = "(no output)"
    body = Text()
    for line in text.splitlines() or [text]:
        body.append("  │ ", style=gutter_style)
        body.append(line + "\n", style=style)
    if extra > 0:
        body.append("  │ ", style=gutter_style)
        body.append(f"… (+{extra} more lines)\n", style=f"{MUTED} italic")
    console.print(body, end="")


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


def diff(text: str) -> None:
    """Render a unified diff with +/- line coloring."""
    if not text.strip():
        return
    lines = text.splitlines()
    shown = lines[:_MAX_DIFF_LINES]
    body = Text()
    for line in shown:
        if line.startswith("+") and not line.startswith("+++"):
            style = "green"
        elif line.startswith("-") and not line.startswith("---"):
            style = "red"
        elif line.startswith("@@"):
            style = "cyan"
        else:
            style = "dim"
        body.append("  " + line + "\n", style=style)
    if len(lines) > len(shown):
        body.append(f"  … (+{len(lines) - len(shown)} more diff lines)\n", style="dim italic")
    console.print(body, end="")
