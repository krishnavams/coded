"""Interactive REPL with slash commands and a permission prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from rich.table import Table

from coded import ui
from coded.agent import Agent
from coded.config import Config, ConfigError

SLASH_COMMANDS = {
    "/help": "Show this help.",
    "/model": "Show or switch the active model: /model [name].",
    "/models": "List all configured models.",
    "/tools": "List available tools.",
    "/cost": "Show token usage and estimated cost this session.",
    "/clear": "Clear conversation history (keep the system prompt).",
    "/reset": "Alias for /clear.",
    "/config": "Show which config files are loaded.",
    "/exit": "Quit (also /quit).",
}


def _history_path() -> Path:
    base = Path.home() / ".config" / "coded"
    base.mkdir(parents=True, exist_ok=True)
    return base / "history"


class Repl:
    def __init__(self, agent: Agent, config: Config):
        self.agent = agent
        self.config = config
        completer = WordCompleter(list(SLASH_COMMANDS) + list(config.models), sentence=True)
        self.prompt = PromptSession(
            history=FileHistory(str(_history_path())),
            completer=completer,
        )
        # Wire the permission prompt into the manager.
        self.agent.permissions.set_prompt(self._permission_prompt)

    # -- permission prompt --------------------------------------------------
    def _permission_prompt(self, title: str, detail: str) -> str:
        ui.console.print()
        ui.console.print(f"[yellow]▶ {title}[/yellow]")
        for line in detail.splitlines() or [detail]:
            ui.console.print(f"  [dim]{line}[/dim]")
        try:
            answer = self.prompt.prompt("  Allow? [y]es / [n]o / [a]lways: ")
        except (EOFError, KeyboardInterrupt):
            return "no"
        return answer or "no"

    # -- main loop ----------------------------------------------------------
    def run(self, initial: Optional[str] = None) -> None:
        ui.banner(self.agent.model.name, self.agent.cwd)
        pending = initial
        while True:
            if pending is not None:
                text = pending
                pending = None
                ui.console.print(f"[bold]›[/bold] {text}")
            else:
                try:
                    text = self.prompt.prompt("› ")
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    break
            text = text.strip()
            if not text:
                continue
            if text.startswith("/"):
                if self._handle_slash(text):
                    break
                continue
            try:
                ui.user_prefix()
                self.agent.run(text)
            except KeyboardInterrupt:
                ui.warn("Interrupted.")
            except ConfigError as exc:
                ui.error(str(exc))
            except Exception as exc:  # noqa: BLE001
                ui.error(f"{type(exc).__name__}: {exc}")
        ui.info("Goodbye.")

    # -- slash commands -----------------------------------------------------
    def _handle_slash(self, text: str) -> bool:
        """Return True to exit the REPL."""
        parts = text.split()
        cmd, args = parts[0], parts[1:]

        if cmd in ("/exit", "/quit"):
            return True
        if cmd == "/help":
            self._cmd_help()
        elif cmd == "/models":
            self._cmd_models()
        elif cmd == "/model":
            self._cmd_model(args)
        elif cmd == "/tools":
            self._cmd_tools()
        elif cmd == "/cost":
            self._cmd_cost()
        elif cmd in ("/clear", "/reset"):
            self.agent.session.reset()
            ui.success("Conversation cleared.")
        elif cmd == "/config":
            self._cmd_config()
        else:
            ui.warn(f"Unknown command: {cmd}. Try /help.")
        return False

    def _cmd_help(self) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        for name, desc in SLASH_COMMANDS.items():
            table.add_row(f"[cyan]{name}[/cyan]", desc)
        ui.console.print(table)

    def _cmd_models(self) -> None:
        table = Table(title="Configured models")
        table.add_column("alias", style="cyan")
        table.add_column("model")
        table.add_column("provider")
        table.add_column("", style="green")
        for name, mc in self.config.models.items():
            active = "● active" if name == self.agent.model.name else ""
            table.add_row(name, mc.model, mc.provider, active)
        ui.console.print(table)

    def _cmd_model(self, args) -> None:
        if not args:
            ui.info(f"Active model: {self.agent.model.name} ({self.agent.model.model})")
            return
        name = args[0]
        try:
            mc = self.config.get_model(name)
        except ConfigError as exc:
            ui.error(str(exc))
            return
        self.agent.switch_model(mc)
        ui.success(f"Switched to '{name}' ({mc.model}).")

    def _cmd_tools(self) -> None:
        table = Table(title="Available tools")
        table.add_column("tool", style="cyan")
        table.add_column("permission")
        table.add_column("description")
        for t in self.agent.registry.all():
            perm = "[yellow]asks[/yellow]" if t.requires_permission else "auto"
            desc = t.description.strip().splitlines()[0]
            table.add_row(t.name, perm, desc[:80])
        ui.console.print(table)

    def _cmd_cost(self) -> None:
        u = self.agent.session.total_usage
        cost = self.agent.session.estimated_cost(self.agent.model)
        ui.console.print(
            f"[dim]Turns:[/dim] {self.agent.session.turns}   "
            f"[dim]Input tokens:[/dim] {u.prompt_tokens:,}   "
            f"[dim]Output tokens:[/dim] {u.completion_tokens:,}   "
            f"[dim]Est. cost:[/dim] ${cost:.4f}"
        )

    def _cmd_config(self) -> None:
        if self.config.source_paths:
            ui.info("Loaded config from:")
            for p in self.config.source_paths:
                ui.console.print(f"  [dim]{p}[/dim]")
        else:
            ui.info("No config files loaded (using defaults / CLI flags / env).")
        if self.config.mcp_servers:
            ui.info(f"MCP servers configured: {', '.join(self.config.mcp_servers)}")
