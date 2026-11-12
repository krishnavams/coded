"""Interactive REPL with slash commands and a permission prompt."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter, merge_completers
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.filters import Condition, has_completions, vi_navigation_mode
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.table import Table

from coded import ui
from coded.agent import Agent
from coded.config import Config, ConfigError, load_config
from coded.typeahead import TypeAhead, default_poll
from coded.llm import explain_failure

# prompt_toolkit color theme (matches ui.py accents).
_PT_STYLE = Style.from_dict({
    "prompt": f"{ui.ACCENT} bold",
    "rule": ui.ACCENT_DEEP,
    "bottom-toolbar": f"{ui.MUTED} bg:#15121f noreverse",
    "bottom-toolbar.accent": f"{ui.ACCENT} bg:#15121f",
    "box": ui.ACCENT_DEEP,
    "completion-menu.completion": "bg:#1c1830 #cfc8e8",
    "completion-menu.completion.current": f"bg:{ui.ACCENT} #120f1d bold",
    "completion-menu.meta.completion": "bg:#1c1830 #7d7699",
    "completion-menu.meta.completion.current": f"bg:{ui.ACCENT_DEEP} #f3efff",
    "auto-suggestion": "#5b5475",
})

def _box_input(session: PromptSession, status) -> bool:
    """Frame the input in a box with the status line right under it.

    prompt_toolkit pins a `bottom_toolbar` to the last row of the terminal, so
    the status bar would float far below the input; its own `show_frame`
    stretches the frame down to it. Instead the layout becomes: top edge,
    `│ input │`, bottom edge, status line, then a filler that takes the spare
    rows (the completion menu opens over it). The edges stay in the scrollback
    after Enter; the status line and the filler do not. Returns False, leaving
    the stock toolbar, if this prompt_toolkit lays its prompt out differently.
    """
    from prompt_toolkit.filters import is_done, to_filter
    from prompt_toolkit.layout import (ConditionalContainer, FloatContainer,
                                       FormattedTextControl, HSplit, VSplit, Window)
    from prompt_toolkit.layout.dimension import Dimension

    try:
        root = session.app.layout.container
        input_window = session.app.layout.current_window
        if not isinstance(root, HSplit) or len(root.children) < 2:
            return False
        children = list(root.children)
        main, rest = children[0], children[1:-1]   # the last child is the toolbar
        # The input with its floats (completion menus, right prompt). The menus
        # move to the whole layout, or they would be clipped to the input row.
        input_floats = main.alternative_content
        if not isinstance(input_floats, FloatContainer):
            return False
        menus = [f for f in input_floats.floats if f.ycursor]
        if not menus:
            return False

        def edge(left: str, right: str) -> VSplit:
            return VSplit([Window(width=1, height=1, char=left, style="class:box"),
                           Window(height=1, char="─", style="class:box"),
                           Window(width=1, height=1, char=right, style="class:box")])

        body = VSplit([Window(width=1, char="│", style="class:box"), Window(width=1, char=" "),
                       main,
                       Window(width=1, char=" "), Window(width=1, char="│", style="class:box")])
        # The input keeps its own height; the spare rows go to the filler.
        input_window.dont_extend_height = to_filter(True)
        status_line = ConditionalContainer(
            Window(FormattedTextControl(status), height=1, style="class:bottom-toolbar"),
            filter=~is_done)
        filler = ConditionalContainer(Window(height=Dimension(min=0)), filter=~is_done)
        session.reserve_space_for_menu = 0   # the menu opens over the filler instead
        input_floats.floats[:] = [f for f in input_floats.floats if not f.ycursor]
        session.app.layout.container = FloatContainer(
            HSplit([edge("╭", "╮"), body, edge("╰", "╯"), status_line, *rest, filler]),
            floats=menus)
        session.bottom_toolbar = None
        return True
    except Exception:  # noqa: BLE001 - a different layout: keep the stock one
        return False


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
    "/permissions": "Show or change what may run without asking: "
                    "/permissions [allow|deny|remove <rule>] [mode <name>] [save].",
    "/budget": "Show or set the session's spending limit: /budget [dollars|off].",
    "/todos": "Show the agent's current task list.",
    "/shells": "List background shells; /shells kill <id>.",
    "/agents": "List outside agents available to delegate to (external_agent tool).",
    "/commands": "List custom slash commands (.coded/commands/*.md).",
    "/plugins": "List installed plugins (manage with `coded plugin …`).",
    "/prompts": "Show which prompt files are in use (override with `coded prompts init`).",
    "/init": "Analyze the repo and write a CODED.md project guide.",
    "/image": "Attach image(s) to your next message: /image <path>... [prompt].",
    "/paste": "Attach the image on the clipboard to your next message (also Ctrl+V).",
    "/vim": "Toggle vi key bindings in the input (config: \"editing_mode\": \"vi\").",
    "/undo": "Undo the last file changes (checkpoint).",
    "/rewind": "Go back to an earlier request (conversation and files): /rewind [n] [--keep-files].",
    "/context": "Show what fills the context window.",
    "/style": "Change how the agent talks: /style [name|off] (no name: list styles).",
    "/add-dir": "Let the agent work in another folder too: /add-dir <path> (no path: list).",
    "/export": "Save the conversation as Markdown: /export [path].",
    "/checkpoints": "List saved checkpoints.",
    "/compact": "Summarize the conversation now to free up context.",
    "/cost": "Show token usage and estimated cost this session.",
    "/clear": "Clear conversation history (keep the system prompt).",
    "/reset": "Alias for /clear.",
    "/config": "Show which config files are loaded.",
    "/setup": "Configure models interactively (adds, tests, roles, pools, routing).",
    "/verify": "Run the project's checks (and the verify model) over the last change.",
    "/memory": "Project memory: /memory [list|add <note>|forget <id>|clear].",
    "/exit": "Quit (also /quit).",
}


KEYS = {
    "@path": "Attach a file (@app.py, @app.py#L10-40), a folder listing or an image. Tab completes.",
    "Alt+Enter / Ctrl+J": "New line without sending (or end the line with \\ and press Enter).",
    "Esc": "Stop the agent mid-turn (Ctrl-C works too).",
    "Shift+Tab": "Cycle mode: normal → accept edits (file edits run without asking) → plan.",
    "Ctrl+G": "Write the message in $EDITOR.",
    "Ctrl+V": "Attach the image on the clipboard.",
    "Ctrl+R": "Search earlier messages.",
    "Tab / →": "Accept the grey suggestion from earlier messages (Tab completes from the menu when it is open).",
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
        self._typeahead = None  # set while a turn runs
        self._prefill = ""  # a line half-typed when the last turn ended
        self._tasks_shown: list = []  # the task list as last shown above the prompt
        self._on_turn = on_turn  # called after each completed turn (autosave)
        self._pending_images: Optional[list] = None
        from coded.notify import Notifier

        self.notifier = Notifier.from_config(config)
        from coded.statusline import StatusLine, status_data

        def _redraw() -> None:
            from prompt_toolkit.application.current import get_app_or_none

            app = get_app_or_none()
            if app is not None and app.is_running:
                app.invalidate()

        self.statusline = StatusLine(config.statusline, lambda: status_data(self.agent),
                                     on_change=_redraw)
        from coded.commands import discover_commands

        self.commands = discover_commands(agent.cwd)

        words = (list(SLASH_COMMANDS) + [f"/{n}" for n in self.commands]
                 + list(config.models) + list(agent.skills))
        meta = dict(SLASH_COMMANDS)
        meta.update({f"/{n}": f"command · {c.description}"[:60] for n, c in self.commands.items()})
        meta.update({name: f"model · {mc.model}" for name, mc in config.models.items()})
        meta.update({name: "skill" for name in agent.skills})
        from coded.mentions import MentionCompleter

        self.mentions = MentionCompleter(agent.cwd)
        completer = merge_completers([WordCompleter(words, meta_dict=meta, sentence=True),
                                      self.mentions])

        bindings = KeyBindings()

        @bindings.add("s-tab", filter=~has_completions)
        def _cycle_mode(event) -> None:  # Shift+Tab: normal → accept edits → plan
            self.agent.cycle_mode()
            event.app.invalidate()  # redraw the status bar

        @Condition
        def _suggestion_shown() -> bool:
            from prompt_toolkit.application.current import get_app

            buf = get_app().current_buffer
            return bool(buf.suggestion and buf.suggestion.text
                        and buf.document.is_cursor_at_the_end)

        # Tab takes the grey suggestion from history (Right arrow does too);
        # with the completion menu open, Tab still completes from the menu.
        @bindings.add("tab", filter=_suggestion_shown & ~has_completions & ~vi_navigation_mode)
        def _accept_suggestion(event) -> None:
            buf = event.current_buffer
            buf.insert_text(buf.suggestion.text)

        # A new line without sending: Alt+Enter, Ctrl+J, or `\` then Enter.
        @bindings.add("escape", "enter")
        @bindings.add("c-j")
        def _newline(event) -> None:
            event.current_buffer.insert_text("\n")

        @Condition
        def _ends_with_backslash() -> bool:
            from prompt_toolkit.application.current import get_app

            buf = get_app().current_buffer
            return buf.document.text_before_cursor.endswith("\\")

        @bindings.add("enter", filter=_ends_with_backslash & ~has_completions)
        def _continue_line(event) -> None:
            buf = event.current_buffer
            buf.delete_before_cursor(1)
            buf.insert_text("\n")

        @bindings.add("c-g")
        def _edit_in_editor(event) -> None:  # Ctrl+G: write the message in $EDITOR
            event.current_buffer.open_in_editor(validate_and_handle=False)

        @bindings.add("c-v")
        def _paste_image(event) -> None:  # Ctrl+V: attach the clipboard's image
            path = self._grab_clipboard()
            if path:
                event.current_buffer.insert_text(f"[image {len(self._pending_images or [])}] ")

        self.prompt = _prompt_session(
            key_bindings=bindings,
            history=FileHistory(str(_history_path())),
            completer=completer,
            auto_suggest=AutoSuggestFromHistory(),
            style=_PT_STYLE,
            bottom_toolbar=self._bottom_toolbar,
            complete_while_typing=True,
            vi_mode=str(config.editing_mode).lower() == "vi",
            # Lines after the first line up under the text, not under `>`.
            prompt_continuation=lambda width, line, wrapped: " " * min(width, 2),
        )
        self._boxed = _box_input(self.prompt, self._bottom_toolbar)
        # Wire the permission prompt into the manager.
        self.agent.permissions.set_prompt(self._permission_prompt)
        self.agent.permissions.set_question(self._question_prompt)

    # -- status bar ---------------------------------------------------------
    def _bottom_toolbar(self):
        custom = self.statusline.text()
        if custom is not None:
            from prompt_toolkit.formatted_text import ANSI

            return ANSI("  " + custom)
        s = self.agent.session
        u = s.total_usage
        cost = s.estimated_cost(self.agent.model)
        tok = u.prompt_tokens + u.completion_tokens
        tok_str = f"{tok/1000:.1f}k" if tok >= 1000 else str(tok)
        compact = "  ⟳ compacted" if getattr(s, "compactions", 0) else ""
        mode = {
            "plan": f"  ·  <style fg='{ui.WARN}'><b>⏸ plan mode</b></style>",
            "accept-edits": f"  ·  <style fg='{ui.ACCENT2}'><b>⏵⏵ accept edits</b></style>",
        }.get(self.agent.permission_mode, "")
        done, total, _ = ui.todo_progress(self.agent.todos.items)
        if not total:
            tasks = ""
        elif done == total:
            tasks = f"  ·  <style fg='{ui.MUTED}'>tasks {done}/{total} done</style>"
        else:
            tasks = (f"  ·  <style fg='{ui.ACCENT_SOFT}'>tasks {done}/{total} "
                     f"{ui.todo_bar(self.agent.todos.items, 8)}</style>")
        return HTML(
            f"  <style fg='{ui.ACCENT}'><b>◆ {self.agent.model.name}</b></style>{mode}"
            f"  ·  {s.turns} turns"
            f"  ·  {tok_str} tokens"
            f"  ·  ~${cost:.4f}{compact}{tasks}"
            f"  ·  @ files  ·  shift+tab mode  ·  /help"
        )

    def _permission_prompt(self, title: str, detail: str) -> str:
        self.notifier.waiting(title)
        ui.console.print()
        ui.permission_panel(title, detail)
        if self._typeahead is not None and self._typeahead.available:
            # A turn is running and the type-ahead reader owns stdin; asking it
            # for the line keeps the two from racing for the same keystrokes.
            # Only a real answer counts here: a follow-up the user happened to
            # send at this moment must not approve an action, nor be lost.
            while True:
                ui.console.print("  [bold]Allow?[/bold] [green]y[/green]es  "
                                 "[red]n[/red]o  [cyan]a[/cyan]lways")
                answer = self._typeahead.ask()
                if answer is None:
                    return "no"
                answer = answer.strip()
                if answer.lower() in ("y", "yes", "n", "no", "a", "always"):
                    return answer
                if answer:
                    self._typeahead.requeue(answer)
                    ui.console.print(f"[{ui.ACCENT_SOFT}]  (queued as a message — "
                                     f"answer y, n or a first)[/]")
        try:
            answer = self.prompt.prompt(
                HTML("  <b>Allow?</b> <ansigreen>y</ansigreen>es  "
                     "<ansired>n</ansired>o  <ansicyan>a</ansicyan>lways "),
                bottom_toolbar=None,
            )
        except (EOFError, KeyboardInterrupt):
            return "no"
        return answer or "no"

    def _question_prompt(self, question: str, options: list) -> Optional[str]:
        """ask_user: show the question, return the answer ('' = skipped)."""
        self.notifier.waiting("Question")
        ui.console.print()
        ui.question_panel(question, options)
        if self._typeahead is not None and self._typeahead.available:
            return ui.pick_answer(self._typeahead.ask() or "", options)
        try:
            answer = self.prompt.prompt(HTML("  <b>Answer:</b> "), bottom_toolbar=None)
        except (EOFError, KeyboardInterrupt):
            return ""
        return ui.pick_answer(answer, options)

    def _grab_clipboard(self) -> Optional[str]:
        """Attach the clipboard's image to the next message; None if there is none."""
        from prompt_toolkit.application import run_in_terminal

        from coded.images import clipboard_hint, grab_clipboard_image

        path = grab_clipboard_image()

        def say() -> None:
            if path:
                ui.info(f"Attached the clipboard image ({Path(path).name}) to your next message.")
            else:
                ui.warn("No image on the clipboard — " + clipboard_hint())

        from prompt_toolkit.application.current import get_app_or_none

        app = get_app_or_none()
        if app is not None and app.is_running:  # Ctrl+V inside the prompt
            run_in_terminal(say)
        else:  # /paste
            say()
        if path:
            self._pending_images = list(self._pending_images or []) + [path]
        return path

    def _escape(self) -> None:
        """Esc during a turn: stop it, as Ctrl-C does."""
        self.agent.interrupt()
        import os
        import signal

        if os.name == "nt":
            import _thread

            _thread.interrupt_main()
        else:
            os.kill(os.getpid(), signal.SIGINT)

    # -- main loop ----------------------------------------------------------
    def _queued_note(self, text: str, waiting: int) -> None:
        short = text if len(text) <= 60 else text[:57] + "…"
        ui.console.print(f"[{ui.ACCENT_SOFT}]  ⏎ queued ({waiting}): {short}[/]")

    def _show_tasks_above_prompt(self) -> bool:
        """Show the task list right above the input, as the TUI docks it.

        Shown while any task is unfinished, and once more when the list has
        changed since it was last shown (so the finished list appears once,
        not before every prompt from then on)."""
        items = [dict(i) for i in self.agent.todos.items]
        done, total, _ = ui.todo_progress(items)
        if not total or (done == total and items == self._tasks_shown):
            return False
        self._tasks_shown = items
        ui.todos(items)
        return True

    def run(self, initial: Optional[str] = None, images: Optional[list] = None) -> None:
        ui.banner(self.agent.model.name, self.agent.cwd)
        from coded.update_check import notice

        update = notice(self.config.update_check)
        if update:
            ui.info(update)
        queued: List[str] = [initial] if initial else []
        self._pending_images = images
        while True:
            if queued:
                text = queued.pop(0)
                ui.echo_user(text)
            else:
                try:
                    ui.console.print()
                    if not self._boxed:
                        ui.console.rule(style=ui.ACCENT_DEEP)
                    self._show_tasks_above_prompt()
                    text = self.prompt.prompt(HTML("<prompt>&gt;</prompt> "),
                                              default=self._prefill)
                    self._prefill = ""
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
                # Keep reading stdin during the turn: anything typed is queued
                # and run next, instead of being swallowed by the busy terminal.
                with TypeAhead(default_poll(), on_queue=self._queued_note,
                               on_escape=self._escape) as ta:
                    self._typeahead = ta
                    self.notifier.start()
                    try:
                        answer = self.agent.run(text, images=self._pending_images)
                        self.notifier.finished(answer)
                    finally:
                        self._typeahead = None
                        queued += ta.drain()
                        self._prefill = ta.partial  # a half-typed line (Windows)
                self._pending_images = None
                if self._on_turn:
                    self._on_turn()
            except KeyboardInterrupt:
                ui.warn("Interrupted.")
                queued.clear()
            except ConfigError as exc:
                ui.error(str(exc))
            except Exception as exc:  # noqa: BLE001
                ui.error(explain_failure(exc))
            if queued:
                ui.info(f"{len(queued)} queued message(s) — running the next one.")
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
        elif cmd == "/setup":
            self._cmd_setup()
        elif cmd == "/verify":
            self._cmd_verify()
        elif cmd == "/memory":
            self._cmd_memory(args)
        elif cmd == "/model":
            self._cmd_model(args)
        elif cmd == "/tools":
            self._cmd_tools()
        elif cmd == "/image":
            self._cmd_image(args)
        elif cmd == "/paste":
            self._grab_clipboard()
        elif cmd == "/vim":
            from prompt_toolkit.enums import EditingMode

            vi = self.prompt.editing_mode != EditingMode.VI
            self.prompt.editing_mode = EditingMode.VI if vi else EditingMode.EMACS
            ui.info(f"{'vi' if vi else 'emacs'} key bindings in the input.")
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
            self.agent.run(_INIT_PROMPT, route_hint="/init")
        elif cmd == "/cost":
            self._cmd_cost()
        elif cmd == "/rewind":
            self._cmd_rewind(args)
        elif cmd == "/style":
            self._cmd_style(args)
        elif cmd == "/add-dir":
            self._cmd_add_dir(args)
        elif cmd == "/context":
            from coded.transcript import context_breakdown, render_context

            ui.console.print(render_context(context_breakdown(self.agent)), markup=False,
                             highlight=False)
        elif cmd == "/export":
            from coded.transcript import write_export

            path = write_export(self.agent, " ".join(args) or None)
            ui.success(f"Saved the conversation to {path}")
        elif cmd == "/permissions":
            self._cmd_permissions(args)
        elif cmd == "/budget":
            self._cmd_budget(args)
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
            self.agent.run(command.expand(arguments),
                           route_hint=f"/{name} {arguments}".strip())
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
            ui.info("No outside agents found. Run `coded agents` in a shell to see which are "
                    "supported and the command to install each, or add `external_agents` "
                    "to config.")
            return
        table = Table(title="Outside agents (external_agent tool)")
        table.add_column("agent", style="cyan")
        table.add_column("command")
        table.add_column("description")
        for a in tool.agents.values():
            table.add_row(a.name, " ".join(a.command), a.description)
        ui.console.print(table)
        ui.info("`coded agents` (in a shell) lists the others and how to install them.")

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

    def _cmd_permissions(self, args: List[str]) -> None:
        """/permissions [allow|deny|remove <rule>] [mode default|accept-edits|plan] [save]"""
        perms = self.agent.permissions
        if args and args[0] in ("allow", "deny") and len(args) > 1:
            rule = " ".join(args[1:])
            added = perms.add_rule(args[0], rule)
            ui.info(f"{args[0].title()} rule {'added' if added else 'already there'}: {rule} "
                    f"(this session; /permissions save keeps it)")
            return
        if args and args[0] == "remove" and len(args) > 1:
            rule = " ".join(args[1:])
            ui.info(f"Removed: {rule}" if perms.remove_rule(rule) else f"No such rule: {rule}")
            return
        if args and args[0] == "mode" and len(args) > 1:
            wanted = args[1]
            if wanted not in ("default", "accept-edits", "plan"):
                ui.warn("Modes: default, accept-edits, plan.")
                return
            while self.agent.permission_mode != wanted:
                self.agent.cycle_mode()
            ui.info(f"Mode: {wanted}.")
            return
        if args and args[0] == "save":
            from coded.config import project_config_path
            from coded.config_wizard import read_file, write_file

            path = project_config_path(self.agent.cwd)
            data = read_file(path)
            data["permissions"] = {"allow": list(perms.allow_rules), "deny": list(perms.deny_rules)}
            write_file(path, data)
            ui.success(f"Saved {len(perms.allow_rules)} allow and {len(perms.deny_rules)} deny "
                       f"rule(s) to {path}.")
            return
        if args:
            ui.warn("Usage: /permissions [allow|deny|remove <rule>] [mode <name>] [save]")
            return
        mode = self.agent.permission_mode
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        table.add_row("[cyan]mode[/cyan]", mode + ("  (yolo: everything runs)"
                                                   if perms.auto_approve else ""))
        table.add_row("[cyan]allow[/cyan]", ", ".join(perms.allow_rules) or "[dim]none[/dim]")
        table.add_row("[cyan]deny[/cyan]", ", ".join(perms.deny_rules) or "[dim]none[/dim]")
        table.add_row("[cyan]always (session)[/cyan]",
                      ", ".join(perms.always_allowed) or "[dim]none[/dim]")
        ui.console.print(table)
        ui.console.print(f"[{ui.MUTED}]Rules look like bash, bash(git *), edit(src/*). "
                         f"Deny always wins. Shift+Tab cycles the mode.[/]")

    def _cmd_style(self, args: List[str]) -> None:
        from coded.styles import discover_styles

        styles = discover_styles(self.agent.cwd)
        if not args:
            current = self.config.output_style or "default"
            table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
            table.add_row(("● " if current == "default" else "  ") + "[cyan]default[/cyan]",
                          "coded's normal voice")
            for name, style in styles.items():
                mark = "● " if name == current else "  "
                table.add_row(f"{mark}[cyan]{name}[/cyan]", style.description)
            ui.console.print(table)
            ui.console.print(f"[{ui.MUTED}]Add your own in .coded/styles/<name>.md.[/]")
            return
        name = args[0]
        if name in ("off", "default", "none"):
            self.config.output_style = None
        elif name in styles:
            self.config.output_style = name
        else:
            ui.warn(f"No style '{name}'. Styles: default, {', '.join(styles)}.")
            return
        self.agent.refresh_system_prompt()
        ui.success(f"Output style: {self.config.output_style or 'default'}.")

    def _cmd_add_dir(self, args: List[str]) -> None:
        import os

        if not args:
            dirs = self.config.add_dirs
            ui.info("Extra folders: " + (", ".join(dirs) if dirs else "none") +
                    "  (/add-dir <path> adds one)")
            return
        path = os.path.abspath(os.path.join(self.agent.cwd, os.path.expanduser(" ".join(args))))
        if not os.path.isdir(path):
            ui.warn(f"{path} is not a folder.")
            return
        if path not in self.config.add_dirs:
            self.config.add_dirs.append(path)
            self.agent.refresh_system_prompt()
        ui.success(f"The agent may now work in {path} too.")

    def _cmd_rewind(self, args: List[str]) -> None:
        """/rewind lists requests; /rewind N goes back to just before request N
        (its text is put back in the input to edit and resend)."""
        keep = "--keep-files" in args
        args = [a for a in args if a != "--keep-files"]
        turns = self.agent.turn_history()
        if not turns:
            ui.info("Nothing to rewind in this conversation.")
            return
        if not args:
            table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
            for n, turn in enumerate(turns, 1):
                text = " ".join(turn["text"].split())
                table.add_row(f"[cyan]{n}[/cyan]", text[:90] + ("…" if len(text) > 90 else ""))
            ui.console.print(table)
            ui.console.print(f"[{ui.MUTED}]/rewind N goes back to just before request N and "
                             f"undoes its file changes (--keep-files leaves files alone).[/]")
            return
        try:
            summary, text = self.agent.rewind(int(args[0]), restore_files=not keep)
        except ValueError as exc:
            ui.warn(str(exc) if "between" in str(exc) else "Usage: /rewind [n] [--keep-files]")
            return
        ui.success(summary)
        self._prefill = text  # edit it and send again, or clear it

    def _cmd_budget(self, args: List[str]) -> None:
        spent = self.agent.session.estimated_cost(self.agent.model)
        if args:
            if args[0] in ("off", "none"):
                self.config.max_cost = None
            else:
                try:
                    self.config.max_cost = float(args[0].lstrip("$"))
                except ValueError:
                    ui.warn("Usage: /budget [dollars|off]")
                    return
        limit = self.config.max_cost
        ui.info(f"Spent ~${spent:.4f}; limit: " + (f"${limit:.2f}" if limit is not None else "none"))

    def _cmd_help(self) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        for name, desc in SLASH_COMMANDS.items():
            table.add_row(f"[cyan]{name}[/cyan]", desc)
        for name, c in self.commands.items():
            table.add_row(f"[cyan]/{name}[/cyan]", f"{c.description} [dim](custom)[/dim]")
        ui.console.print(table)
        keys = Table(show_header=False, box=None, padding=(0, 2, 0, 0), title="Keys",
                     title_justify="left")
        for key, desc in KEYS.items():
            keys.add_row(f"[cyan]{key}[/cyan]", desc)
        ui.console.print()
        ui.console.print(keys)

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

    def _cmd_setup(self) -> None:
        """Run the wizard, then fold the result into the live session.

        The model-related fields are updated **in place** on the Config the
        agent already holds: replacing the object would drop the CLI overrides
        (--yolo, --max-turns, --config …) applied at startup, and anything
        still holding the old one would not see the change.
        """
        from coded.config_wizard import run_wizard

        run_wizard(cwd=self.agent.cwd)
        try:
            fresh = load_config(cwd=self.agent.cwd)
        except ConfigError as exc:
            ui.error(f"Could not reload the config: {exc}")
            return
        for attr in ("models", "default_model", "model_roles", "pools", "routing", "verify"):
            setattr(self.config, attr, getattr(fresh, attr))
        for warning in self.config.model_warnings():
            ui.warn(warning)
        active = self.agent.model.name
        if active in self.config.models:
            # Re-select without pinning, so routing configured in the wizard stays on.
            self.agent.switch_model(self.config.models[active], pin=False)
        else:
            ui.warn(f"The active model '{active}' is no longer configured; "
                    "switch with /model.")
        ui.info("Config reloaded. /models shows the result.")

    def _cmd_verify(self) -> None:
        """Run the verification gate now, on whatever the last turn changed."""
        from coded.verify import VerifySettings

        settings = VerifySettings.from_config(self.config.verify)
        has_reviewer = bool(self.config.model_roles.get("verify"))
        if not (settings.runs_checks() or settings.runs_review(has_reviewer)):
            ui.info('Verification is not configured. Set "verify": {"command": "pytest -q"} '
                    'in your config (and model_roles.verify for a second opinion), or run '
                    "/setup.")
            return
        changed = self.agent._turn_changed_things
        self.agent._turn_changed_things = True   # verify on demand, change or not
        self.agent._verify_rounds = 0
        try:
            feedback = self.agent._verify_turn(None)
        finally:
            self.agent._turn_changed_things = changed
        if not feedback:
            ui.success("Verification passed.")
            return
        ui.warn("Verification found something:")
        ui.console.print(feedback)
        if self.agent.permissions.ask("Send this back to the agent to fix?", feedback[:400]) \
                in ("y", "yes"):
            ui.user_prefix()
            self.agent.run(feedback)

    def _cmd_memory(self, args) -> None:
        """Project memory, under the user's hand: list, add, forget, clear."""
        from coded.memory import KINDS, MemoryStore

        store = MemoryStore.for_project(self.agent.cwd)
        action = (args[0].lower() if args else "list")
        rest = " ".join(args[1:]).strip()
        if action == "list":
            entries = store.load()
            if not entries:
                ui.info(f"No project memory yet ({store.path}). "
                        "Add one with /memory add <note>.")
                return
            table = Table(title=f"Project memory ({len(entries)})")
            table.add_column("id", style="cyan")
            table.add_column("kind")
            table.add_column("note")
            table.add_column("by", style="dim")
            for e in entries:
                table.add_row(e.id, e.kind, e.text, e.source)
            ui.console.print(table)
        elif action == "add":
            if not rest:
                ui.error("Nothing to add: /memory add <note>  (prefix with "
                         f"{'|'.join(KINDS)}: to set the kind)")
                return
            kind = "fact"
            for k in KINDS:
                if rest.lower().startswith(k + ":"):
                    kind, rest = k, rest[len(k) + 1:].strip()
                    break
            entry = store.add(rest, kind, source="user")
            if entry is None:
                ui.info("Already known; nothing added.")
            else:
                ui.success(f"Remembered as {entry.id}. "
                           "It reaches the model at the start of the next session.")
                self.agent.refresh_system_prompt()
        elif action == "forget":
            if not rest:
                ui.error("Which one? /memory forget <id>")
            elif store.forget(rest):
                ui.success(f"Forgot {rest}.")
                self.agent.refresh_system_prompt()
            else:
                ui.error(f"No note '{rest}'.")
        elif action == "clear":
            count = store.clear()
            ui.success(f"Cleared {count} note(s).")
            self.agent.refresh_system_prompt()
        else:
            ui.error("Usage: /memory [list|add <note>|forget <id>|clear]")

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
        self.agent.run(message, route_hint=f"/skill {name} {task}".strip())

    def _cmd_cost(self) -> None:
        u = self.agent.session.total_usage
        cost = self.agent.session.estimated_cost(self.agent.model)
        cached = f" [dim](cached {u.cached_tokens:,})[/dim]" if u.cached_tokens else ""
        ui.console.print(
            f"[dim]Turns:[/dim] {self.agent.session.turns}   "
            f"[dim]Input tokens:[/dim] {u.prompt_tokens:,}{cached}   "
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
