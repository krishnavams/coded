"""Terminal rendering helpers built on `rich`."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

console = Console()

# Truncation limits for displaying tool activity (the model still sees full output).
_MAX_ARG_LEN = 300
_MAX_RESULT_LINES = 12
_MAX_RESULT_CHARS = 1600


def banner(model_name: str, cwd: str) -> None:
    body = Text()
    body.append("coded", style="bold cyan")
    body.append("  — a coding agent for OpenAI-compatible models\n\n", style="dim")
    body.append("model: ", style="dim")
    body.append(f"{model_name}\n", style="green")
    body.append("cwd:   ", style="dim")
    body.append(f"{cwd}\n\n", style="white")
    body.append("Type your request, or /help for commands. Ctrl-C to interrupt, /exit to quit.", style="dim")
    console.print(Panel(body, border_style="cyan", expand=False))


def user_prefix() -> None:
    console.print()


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
    label.append("⚙ ", style="yellow")
    label.append(name, style="bold yellow")
    label.append("  ")
    label.append(_short_args(args), style="dim")
    console.print(label)


def tool_result(content: str, is_error: bool = False) -> None:
    lines = content.splitlines()
    shown = lines[:_MAX_RESULT_LINES]
    text = "\n".join(shown)
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "…"
    extra = len(lines) - len(shown)
    style = "red" if is_error else "dim"
    prefix = "  ✗ " if is_error else "  ↳ "
    if not text.strip():
        text = "(no output)"
    body = Text()
    for i, line in enumerate(text.splitlines() or [text]):
        body.append(prefix if i == 0 else "    ")
        body.append(line + "\n", style=style)
    if extra > 0:
        body.append(f"    … (+{extra} more lines)\n", style="dim italic")
    console.print(body, end="")


def info(msg: str) -> None:
    console.print(f"[dim]{msg}[/dim]")


def warn(msg: str) -> None:
    console.print(f"[yellow]! {msg}[/yellow]")


def error(msg: str) -> None:
    console.print(f"[red]✗ {msg}[/red]")


def success(msg: str) -> None:
    console.print(f"[green]✓ {msg}[/green]")


def rule(msg: str = "") -> None:
    console.rule(f"[dim]{msg}[/dim]" if msg else "")
