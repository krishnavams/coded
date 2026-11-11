"""Interactive REPL with slash commands and a permission prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.filters import has_completions
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.table import Table

from coded import ui
from coded.agent import Agent
from coded.config import Config, ConfigError

# prompt_toolkit color theme (matches ui.py accents).
_PT_STYLE = Style.from_dict({
    "prompt": f"{ui.ACCENT} bold",
    "rule": ui.ACCENT_DEEP,
    "bottom-toolbar": f"{ui.MUTED} bg:#15121f noreverse",
    "bottom-toolbar.accent": f"{ui.ACCENT} bg:#15121f",
    "completion-menu.completion": "bg:#1c1830 #cfc8e8",
    "completion-menu.completion.current": f"bg:{ui.ACCENT} #120f1d bold",
    "completion-menu.meta.completion": "bg:#1c1830 #7d7699",
    "completion-menu.meta.completion.current": f"bg:{ui.ACCENT_DEEP} #f3efff",
    "auto-suggestion": "#5b5475",
})

def _prompt_session(**kwargs) -> PromptSession:
    """A PromptSession, with a readable error where there is no console.

    On Windows, prompt_toolkit cannot build its output when stdout is not a
    console (a redirected or embedded terminal); the interactive REPL needs one.
    """
    try:
        return PromptSession(**kwargs)
    except Exception as exc:  # noqa: BLE001 - NoConsoleScreenBufferError and friends
        if type(exc).__name__ != "NoConsoleScreenBufferError":
            raise
        raise ConfigError(
            "coded's interactive prompt needs a Windows console. Run it in Windows "
            "Terminal, PowerShell or the Command Prompt; in a Git Bash (mintty) "
            "window use `winpty coded`. One-shot mode works anywhere: coded -p \"…\"."
        ) from exc


SLASH_COMMANDS = {
    "/help": "Show this help.",
    "/model": "Show or switch the active model: /model [name].",
    "/models": "List all configured models and pools.",
    "/route": "Difficulty routing: /route [on|off] (shows the current state).",
    "/tools": "List available tools.",
    "/skills": "List available skills.",
    "/skill": "Invoke a skill now: /skill <name> [task].",
    "/review": "Run reviewer→qa→security sub-agents on the changes: /review [target].",
    "/plan": "Toggle plan mode (read-only research, then approve a plan). Also Shift+Tab.",
    "/todos": "Show the agent's current task list.",
    "/shells": "List background shells; /shells kill <id>.",
    "/agents": "List outside agents available to delegate to (external_agent tool).",
    "/commands": "List custom slash commands (.coded/commands/*.md).",
    "/plugins": "List installed plugins (manage with `coded plugin …`).",
    "/prompts": "Show which prompt files are in use (override with `coded prompts init`).",
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
        from coded.commands import discover_commands

        self.commands = discover_commands(agent.cwd)

        words = (list(SLASH_COMMANDS) + [f"/{n}" for n in self.commands]
                 + list(config.models) + list(agent.skills))
        meta = dict(SLASH_COMMANDS)
        meta.update({f"/{n}": f"command · {c.description}"[:60] for n, c in self.commands.items()})
        meta.update({name: f"model · {mc.model}" for name, mc in config.models.items()})
        meta.update({name: "skill" for name in agent.skills})
        completer = WordCompleter(words, meta_dict=meta, sentence=True)

        bindings = KeyBindings()

        @bindings.add("s-tab", filter=~has_completions)
        def _toggle_plan(event) -> None:  # Shift+Tab: plan mode on/off
            self.agent.set_mode("default" if self.agent.mode == "plan" else "plan",
                                announce=False)
            event.app.invalidate()  # redraw the status bar

        self.prompt = _prompt_session(
            key_bindings=bindings,
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
        mode = ("  ·  <style fg='%s'><b>⏸ plan mode</b></style>" % ui.WARN
                if self.agent.mode == "plan" else "")
        return HTML(
            f"  <style fg='{ui.ACCENT}'><b>◆ {self.agent.model.name}</b></style>{mode}"
            f"  ·  {s.turns} turns"
            f"  ·  {tok_str} tokens"
            f"  ·  ~${cost:.4f}{compact}"
            f"  ·  shift+tab plan  ·  /help"
        )

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
                    ui.console.print()
                    ui.console.rule(style=ui.ACCENT_DEEP)
                    text = self.prompt.prompt(HTML("<prompt>&gt;</prompt> "))
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
        elif cmd == "/plan":
            self.agent.set_mode("default" if self.agent.mode == "plan" else "plan")
            if args:
                ui.user_prefix()
                self.agent.run(" ".join(args))
        elif cmd == "/route":
            self._cmd_route(args)
        elif cmd == "/todos":
            items = self.agent.todos.items
            ui.todos(items) if items else ui.info("No tasks yet.")
        elif cmd == "/shells":
            self._cmd_shells(args)
        elif cmd == "/agents":
            self._cmd_agents()
        elif cmd == "/commands":
            self._cmd_commands()
        elif cmd == "/plugins":
            self._cmd_plugins()
        elif cmd == "/prompts":
            from coded.prompt_files import prompts_main

            prompts_main(["list", "--cwd", self.agent.cwd])
        elif cmd[1:] in self.commands:
            self._run_custom_command(cmd[1:], text[len(cmd):])
        else:
            ui.warn(f"Unknown command: {cmd}. Try /help.")
        return False

    def _run_custom_command(self, name: str, arguments: str) -> None:
        command = self.commands[name]
        previous = None
        if command.model:
            try:
                mc = self.config.get_model(command.model)
            except ConfigError as exc:
                ui.error(str(exc))
                return
            previous = self.agent.model
            self.agent.force_model(mc)  # beats routing for this one request
        try:
            ui.user_prefix()
            self.agent.run(command.expand(arguments))
        finally:
            if previous is not None:
                self.agent.use_model(previous)

    def _cmd_shells(self, args) -> None:
        if args[:1] == ["kill"] and len(args) > 1:
            ok = self.agent.shells.kill(args[1])
            (ui.success if ok else ui.error)(f"{'Stopped' if ok else 'No shell'} {args[1]}.")
            return
        shells = self.agent.shells.list()
        if not shells:
            ui.info("No background shells.")
            return
        table = Table(title="Background shells")
        table.add_column("id", style="cyan")
        table.add_column("status")
        table.add_column("command")
        for sid, cmd, status in shells:
            table.add_row(sid, status, cmd[:80])
        ui.console.print(table)

    def _cmd_agents(self) -> None:
        tool = self.agent.registry.get("external_agent")
        if tool is None:
            ui.info("No outside agents found. Install e.g. Claude Code (claude), Codex (codex), "
                    "Gemini CLI (gemini) or Aider (aider), or add `external_agents` to config.")
            return
        table = Table(title="Outside agents (external_agent tool)")
        table.add_column("agent", style="cyan")
        table.add_column("command")
        table.add_column("description")
        for a in tool.agents.values():
            table.add_row(a.name, " ".join(a.command), a.description)
        ui.console.print(table)

    def _cmd_plugins(self) -> None:
        from coded.plugins import installed_plugins

        plugins = installed_plugins(self.agent.cwd, include_disabled=True)
        if not plugins:
            ui.info("No plugins installed. From a shell: `coded plugin install <dir|zip|git|owner/repo>` "
                    "or `coded plugin marketplace add <source>` then `coded plugin search`.")
            return
        table = Table(title="Plugins")
        for col in ("plugin", "version", "scope", "status", "description"):
            table.add_column(col)
        for p in plugins:
            table.add_row(p.name, p.version, p.scope,
                          "[green]enabled[/green]" if p.enabled else "[dim]disabled[/dim]",
                          p.description[:60])
        ui.console.print(table)
        ui.info("Install/remove/enable with `coded plugin …` in a shell, then restart coded.")

    def _cmd_commands(self) -> None:
        if not self.commands:
            ui.info("No custom commands. Add Markdown prompts under ./.coded/commands/<name>.md "
                    "or ~/.config/coded/commands/<name>.md, then run /<name> [args].")
            return
        table = Table(title="Custom commands")
        table.add_column("command", style="cyan")
        table.add_column("args")
        table.add_column("description")
        for n, c in self.commands.items():
            table.add_row(f"/{n}", c.argument_hint, c.description)
        ui.console.print(table)

    def _cmd_help(self) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        for name, desc in SLASH_COMMANDS.items():
            table.add_row(f"[cyan]{name}[/cyan]", desc)
        for name, c in self.commands.items():
            table.add_row(f"[cyan]/{name}[/cyan]", f"{c.description} [dim](custom)[/dim]")
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
        pool = None
        if name not in self.config.models and self.config.pool_members(name):
            mc, pool = self.config.resolve_target(name)
        else:
            try:
                mc = self.config.get_model(name)
            except ConfigError as exc:
                ui.error(str(exc))
                return
        self.agent.switch_model(mc, pool)
        where = f"pool '{pool}' → '{mc.name}'" if pool else f"'{name}'"
        ui.success(f"Switched to {where} ({mc.model})."
                   + (" Routing is off; /route on to re-enable."
                      if self.config.routing_settings().configured else ""))

    def _cmd_route(self, args) -> None:
        settings = self.config.routing_settings()
        if not settings.configured:
            ui.info('Difficulty routing is not configured. Add e.g. "routing": '
                    '{"enabled": true, "simple": "small", "hard": "large"} to your config.')
            return
        if args and args[0] in ("on", "off"):
            self.agent.set_routing(args[0] == "on")
        targets = ", ".join(f"{lvl}→{settings.target(lvl)}" for lvl in ("simple", "medium", "hard")
                            if settings.target(lvl))
        state = "on" if self.agent.routing_on else "off (an explicit /model pinned it)"
        seen = self.agent._route_level or "—"
        ui.info(f"Routing {state}. {targets}. Highest level this session: {seen}. "
                f"Current model: {self.agent.model.name}.")

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
            body = skill.load_body(self.agent.model)
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
