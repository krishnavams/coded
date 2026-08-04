"""Interactive REPL with slash commands and a permission prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.table import Table

from coded import ui
from coded.agent import Agent
from coded.config import Config, ConfigError

# prompt_toolkit color theme (matches ui.py accents).
_PT_STYLE = Style.from_dict({
    "prompt": "#7c9cff bold",
    "bottom-toolbar": "#8a94a7 bg:#161922",
    "bottom-toolbar.accent": "#5ad1a0 bg:#161922",
    "completion-menu.completion": "bg:#161922 #c0c8d8",
    "completion-menu.completion.current": "bg:#7c9cff #0b0e14 bold",
    "completion-menu.meta.completion": "bg:#161922 #6b7488",
    "completion-menu.meta.completion.current": "bg:#5f78c9 #0b0e14",
    "auto-suggestion": "#4a5568",
})

SLASH_COMMANDS = {
    "/help": "Show this help.",
    "/model": "Show or switch the active model: /model [name].",
    "/models": "List all configured models.",
    "/tools": "List available tools.",
    "/skills": "List available skills.",
    "/skill": "Invoke a skill now: /skill <name> [task].",
    "/review": "Run reviewer→qa→security sub-agents on the changes: /review [target].",
    "/init": "Analyze the repo and write a CODED.md project guide.",
    "/image": "Attach image(s) to your next message: /image <path>... [prompt].",
    "/undo": "Undo the last file changes (checkpoint).",
    "/checkpoints": "List saved checkpoints.",
    "/compact": "Summarize the conversation now to free up context.",
    "/cost": "Show token usage and estimated cost this session.",
    "/clear": "Clear conversation history (keep the system prompt).",
    "/reset": "Alias for /clear.",
    "/config": "Show which config files are loaded.",
    "/exit": "Quit (also /quit).",
}


_INIT_PROMPT = (
    "Analyze this repository and create a CODED.md file that documents it for future "
    "AI assistants and contributors. First explore the layout with ls/glob and read the "
    "key files (package manifests, entry points, config, tests). Then write CODED.md "
    "covering: a one-paragraph project overview, the tech stack, how to install/build/"
    "test/run it, the directory structure, and the important conventions. Verify that any "
    "commands you list actually exist in the repo before documenting them. Keep it concise."
)


def _history_path() -> Path:
    base = Path.home() / ".config" / "coded"
    base.mkdir(parents=True, exist_ok=True)
    return base / "history"


class Repl:
    def __init__(self, agent: Agent, config: Config, on_turn=None):
        self.agent = agent
        self.config = config
        self._on_turn = on_turn  # called after each completed turn (autosave)
        self._pending_images: Optional[list] = None

        words = list(SLASH_COMMANDS) + list(config.models) + list(agent.skills)
        meta = dict(SLASH_COMMANDS)
        meta.update({name: f"model · {mc.model}" for name, mc in config.models.items()})
        meta.update({name: "skill" for name in agent.skills})
        completer = WordCompleter(words, meta_dict=meta, sentence=True)

        self.prompt = PromptSession(
            history=FileHistory(str(_history_path())),
            completer=completer,
            auto_suggest=AutoSuggestFromHistory(),
            style=_PT_STYLE,
            bottom_toolbar=self._bottom_toolbar,
            complete_while_typing=True,
        )
        # Wire the permission prompt into the manager.
        self.agent.permissions.set_prompt(self._permission_prompt)

    # -- status bar ---------------------------------------------------------
    def _bottom_toolbar(self):
        s = self.agent.session
        u = s.total_usage
        cost = s.estimated_cost(self.agent.model)
        tok = u.prompt_tokens + u.completion_tokens
        tok_str = f"{tok/1000:.1f}k" if tok >= 1000 else str(tok)
        compact = "  ⟳ compacted" if getattr(s, "compactions", 0) else ""
        return HTML(
            f"  <b>{self.agent.model.name}</b>"
            f"   turns {s.turns}"
            f"   tokens {tok_str}"
            f"   ~${cost:.4f}{compact}"
            f"   <b>/help</b>"
        )

    # -- permission prompt --------------------------------------------------
    def _permission_prompt(self, title: str, detail: str) -> str:
        ui.console.print()
        ui.permission_panel(title, detail)
        try:
            answer = self.prompt.prompt(
                HTML("  <b>Allow?</b> <ansigreen>y</ansigreen>es  "
                     "<ansired>n</ansired>o  <ansicyan>a</ansicyan>lways "),
                bottom_toolbar=None,
            )
        except (EOFError, KeyboardInterrupt):
            return "no"
        return answer or "no"

    # -- main loop ----------------------------------------------------------
    def run(self, initial: Optional[str] = None, images: Optional[list] = None) -> None:
        ui.banner(self.agent.model.name, self.agent.cwd)
        pending = initial
        self._pending_images = images
        while True:
            if pending is not None:
                text = pending
                pending = None
                ui.echo_user(text)
            else:
                try:
                    text = self.prompt.prompt(HTML("<prompt>❯</prompt> "))
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
                self.agent.run(text, images=self._pending_images)
                self._pending_images = None
                if self._on_turn:
                    self._on_turn()
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
        elif cmd == "/image":
            self._cmd_image(args)
        elif cmd == "/undo":
            ui.info(self.agent.checkpoints.undo_last() if self.agent.checkpoints else "Checkpoints disabled.")
        elif cmd == "/checkpoints":
            self._cmd_checkpoints()
        elif cmd == "/compact":
            if self.agent._maybe_compact(force=True):
                ui.success("Conversation compacted.")
            else:
                ui.info("Nothing to compact yet.")
        elif cmd == "/skills":
            self._cmd_skills()
        elif cmd == "/skill":
            self._cmd_skill(args)
        elif cmd == "/review":
            self._cmd_review(args)
        elif cmd == "/init":
            ui.user_prefix()
            self.agent.run(_INIT_PROMPT)
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

    def _cmd_image(self, args) -> None:
        if not args:
            ui.info("Usage: /image <path> [more paths] [-- prompt] "
                    "(or: /image <path> your prompt here)")
            return
        # Split image paths from an optional trailing prompt at '--', else treat
        # tokens that exist as files as images and the rest as the prompt.
        paths, rest = [], []
        if "--" in args:
            idx = args.index("--")
            paths, rest = args[:idx], args[idx + 1:]
        else:
            for i, tok in enumerate(args):
                if Path(tok).expanduser().is_file():
                    paths.append(tok)
                else:
                    rest = args[i:]
                    break
        if not paths:
            ui.error("No existing image file found in arguments.")
            return
        if not self.agent.model.supports_vision:
            ui.warn(f"Model '{self.agent.model.name}' is not marked supports_vision; "
                    "images may be ignored.")
        self._pending_images = paths
        ui.success(f"Attached {len(paths)} image(s) to your next message.")
        prompt = " ".join(rest).strip()
        if prompt:
            ui.user_prefix()
            self.agent.run(prompt, images=self._pending_images)
            self._pending_images = None

    def _cmd_review(self, args) -> None:
        from coded.review import REVIEW_PHASES, run_review

        if not self.agent.skills or not any(p in self.agent.skills for p in REVIEW_PHASES):
            ui.error("Review roles unavailable (skills disabled?). Expected: "
                     + ", ".join(REVIEW_PHASES))
            return
        target = " ".join(args).strip() or None
        ui.info("Running review pipeline: " + " → ".join(REVIEW_PHASES))
        try:
            run_review(self.agent, target, verbose=True)
        except KeyboardInterrupt:
            ui.warn("Review interrupted.")

    def _cmd_checkpoints(self) -> None:
        cp = self.agent.checkpoints
        groups = cp.list_groups() if cp else []
        if not groups:
            ui.info("No checkpoints yet.")
            return
        table = Table(title="Checkpoints (most recent last)")
        table.add_column("label")
        table.add_column("files", justify="right")
        for g in groups:
            table.add_row(g.get("label", ""), str(len(g.get("files", []))))
        ui.console.print(table)
        ui.info("Use /undo to revert the most recent one.")

    def _cmd_skills(self) -> None:
        if not self.agent.skills:
            ui.info("No skills found. Add them under ./.coded/skills/<name>/SKILL.md "
                    "or ~/.config/coded/skills/<name>/SKILL.md.")
            return
        table = Table(title="Available skills")
        table.add_column("skill", style="cyan")
        table.add_column("description")
        for name, s in self.agent.skills.items():
            table.add_row(name, s.description or "(no description)")
        ui.console.print(table)

    def _cmd_skill(self, args) -> None:
        if not args:
            self._cmd_skills()
            ui.info("Usage: /skill <name> [task]")
            return
        name = args[0]
        if name not in self.agent.skills:
            ui.error(f"Unknown skill '{name}'. Try /skills.")
            return
        skill = self.agent.skills[name]
        try:
            body = skill.load_body()
        except OSError as exc:
            ui.error(f"Could not read skill: {exc}")
            return
        task = " ".join(args[1:]).strip()
        message = (
            f"Apply the skill '{name}'. Its instructions are below; follow them"
            + (f" for this task: {task}\n\n" if task else ".\n\n")
            + f"---\n{body}"
        )
        ui.user_prefix()
        self.agent.run(message)

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
