"""A full-screen terminal UI (Textual) for coded.

Layout:
  ┌ transcript (scrollable) ──────────────┬ side panel (Ctrl+B) ─┐
  │ welcome card, requests, tool calls,   │ session · context    │
  │ diffs, answers                        │ files changed        │
  │                                       │ queued · shells      │
  └───────────────────────────────────────┴──────────────────────┘
  task list (while the agent has one), docked right above the input
  ╭ input ─────────────────────────────────────────────╮
  status line: model · mode · turns · tokens · cost · key hints

The side panel opens by itself on a wide terminal and is drawn by
`coded.sidebar`, which needs no Textual.

The synchronous agent runs in a worker thread; it emits structured events
(via Agent.on_event) that are marshalled onto the UI thread to build widgets.
Permission requests open a modal and block the worker until answered.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from rich.markup import escape as _esc
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    Markdown,
    Static,
)

from coded import sidebar, ui
from coded.agent import Agent
from coded.llm import explain_failure


# ---------------------------------------------------------------------------
# Widgets for the transcript
# ---------------------------------------------------------------------------
class Welcome(Static):
    def __init__(self, model_name: str, cwd: str):
        super().__init__(ui.welcome(model_name, cwd), classes="welcome")


class UserMessage(Static):
    def __init__(self, text: str):
        body = Text()
        body.append("> ", style=f"bold {ui.ACCENT}")
        body.append(text, style="bold")
        super().__init__(body, classes="msg user")


class ToolCall(Static):
    def __init__(self, name: str, args: dict, cwd: str = ""):
        super().__init__(ui.tool_call_text(name, args, cwd), classes="toolcall")


class ToolResult(Static):
    def __init__(self, content: str, is_error: bool, name: str = "", cwd: str = ""):
        body = ui.tool_result_text(content, is_error, name, cwd)
        body.rstrip()
        super().__init__(body, classes="toolresult error" if is_error else "toolresult")


class DiffView(Static):
    def __init__(self, diff: str):
        body = ui.diff_text(diff)
        body.rstrip()
        super().__init__(body, classes="diff")


class StatusNote(Static):
    def __init__(self, text: str):
        super().__init__(Text("  " + text, style=f"{ui.MUTED} italic"), classes="statusnote")


# ---------------------------------------------------------------------------
# Permission modal
# ---------------------------------------------------------------------------
class PermissionModal(ModalScreen[str]):
    def __init__(self, title: str, detail: str):
        super().__init__()
        self._title = title
        self._detail = detail

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="perm-box"):
            yield Label(f"[b {ui.ACCENT}]{_esc(self._title)}[/]", id="perm-title")
            yield Static(Text(self._detail), id="perm-detail")
            with Horizontal(id="perm-buttons"):
                yield Button("Allow (y)", variant="success", id="yes")
                yield Button("Deny (n)", variant="error", id="no")
                yield Button("Always (a)", variant="primary", id="always")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss({"yes": "yes", "no": "no", "always": "always"}[event.button.id])

    def on_key(self, event) -> None:
        mapping = {"y": "yes", "n": "no", "a": "always", "escape": "no"}
        if event.key in mapping:
            event.stop()
            self.dismiss(mapping[event.key])


class QuestionModal(ModalScreen[str]):
    """ask_user: the agent's question, with its choices and a free answer."""

    def __init__(self, question: str, options: list):
        super().__init__()
        self._question = question
        self._options = list(options)

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="perm-box"):
            yield Label(f"[b {ui.ACCENT}]? Question[/]", id="perm-title")
            yield Static(Text(self._question), id="perm-detail")
            if self._options:
                with Vertical(id="question-options"):
                    for i, opt in enumerate(self._options):
                        yield Button(f"{i + 1}. {opt}", id=f"opt-{i}")
            yield Input(placeholder="Your own answer (Enter to send, Esc to skip)",
                        id="question-input")

    def on_mount(self) -> None:
        self.query_one("#question-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self._options[int(event.button.id.split("-", 1)[1])])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(ui.pick_answer(event.value, self._options) or "")

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss("")


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------
class CodedTUI(App):
    CSS = f"""
    Screen {{ background: #100d1a; }}
    #transcript {{ height: 1fr; padding: 0 1; scrollbar-color: {ui.ACCENT_DEEP}; }}
    .welcome {{ margin: 1 0 0 0; }}
    .msg.user {{ background: #1c1830; color: #f3efff; padding: 0 1; margin: 1 0 0 0; }}
    .assistant {{ padding: 0 0; margin: 1 0 0 0; background: #100d1a; }}
    .assistant MarkdownFence {{ background: #181426; }}
    .assistant MarkdownBlock > .code_inline {{ color: {ui.ACCENT_SOFT}; background: #1c1830; }}
    .assistant MarkdownBullet {{ color: {ui.ACCENT}; }}
    .toolcall {{ margin: 1 0 0 0; }}
    .toolresult {{ color: {ui.MUTED}; }}
    .toolresult.error {{ color: {ui.ERR}; }}
    .statusnote {{ margin: 1 0 0 0; }}
    #main {{ height: 1fr; }}
    #left {{ width: 1fr; }}
    #sidebar {{ width: {sidebar.PANEL_WIDTH}; display: none; padding: 1 1;
                border-left: tall #2a2342; background: #120f1f;
                scrollbar-color: {ui.ACCENT_DEEP}; }}
    #sidebar.open {{ display: block; }}
    #tasks {{ height: auto; max-height: 12; padding: 0 2; margin: 1 1 0 1; display: none; }}
    #tasks.shown {{ display: block; }}
    #prompt {{ border: round #3b3260; background: #100d1a; margin: 0 1; }}
    #prompt:focus {{ border: round {ui.ACCENT}; }}
    #statusline {{ height: 1; padding: 0 2; color: {ui.MUTED}; }}
    #perm-box {{ background: #181426; border: round {ui.ACCENT}; padding: 1 2; width: 76; height: auto; }}
    #perm-buttons {{ height: auto; align: center middle; padding-top: 1; }}
    #question-options {{ height: auto; padding-top: 1; }}
    #question-options Button {{ width: 100%; margin: 0 0 0 0; }}
    #question-input {{ margin-top: 1; }}
    PermissionModal {{ align: center middle; background: #100d1a 60%; }}
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+l", "clear", "Clear"),
        Binding("ctrl+u", "undo", "Undo edits"),
        Binding("shift+tab", "toggle_plan", "Mode", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "Side panel", priority=True),
    ]

    busy = reactive(False)

    def __init__(self, agent: Agent, on_turn=None):
        super().__init__()
        self.agent = agent
        self._on_turn = on_turn
        self.agent.on_event = self._agent_event  # called from the worker thread
        self.agent.permissions.set_prompt(self._permission_prompt, threadsafe=True)
        self.agent.permissions.set_question(self._question_prompt)
        self._assistant_md: Optional[Markdown] = None
        self._assistant_buf = ""
        self._thought: Optional[Static] = None
        self._thought_buf = ""
        self._queued: list = []  # messages typed while a turn was running
        self._panel_choice: Optional[bool] = None  # None: follow the terminal width
        self._changes: list = []  # sidebar.FileChange, recomputed after tool results
        from coded import notify

        def _send(title: str, message: str, mode: str) -> str:
            if mode == "bell":  # Textual owns the terminal: ring through it
                self.call_from_thread(self.bell)
                return "bell"
            return notify.send(title, message, mode)

        from coded.statusline import StatusLine, status_data

        self.statusline = StatusLine(
            getattr(agent.config, "statusline", None), lambda: status_data(self.agent),
            on_change=lambda: self.call_from_thread(self._refresh_sidebar))
        self.notifier = notify.Notifier(getattr(agent.config, "notify", "bell"),
                                        getattr(agent.config, "notify_after", 30.0),
                                        sender=_send)
        from coded.commands import discover_commands

        self.commands = discover_commands(agent.cwd)

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield VerticalScroll(id="transcript")
            with VerticalScroll(id="sidebar"):
                yield Static("", id="side-body")
        # Docked by the input, panel open or not, so progress stays in view.
        yield Static("", id="tasks")
        yield Input(placeholder="Ask coded to build, fix, explain…", id="prompt")
        yield Static(self._status_text(), id="statusline")

    def on_mount(self) -> None:
        try:
            from textual.theme import Theme

            self.register_theme(Theme(
                name="coded", primary=ui.ACCENT, secondary=ui.ACCENT_DEEP, accent=ui.ACCENT_SOFT,
                warning=ui.WARN, error=ui.ERR, success=ui.ACCENT2, foreground="#e9e4f7",
                background="#100d1a", surface="#181426", panel="#1c1830", dark=True))
            self.theme = "coded"
        except Exception:  # noqa: BLE001 - older Textual without themes
            pass
        self.title = "coded"
        self.sub_title = f"{self.agent.model.name}  ·  {self.agent.cwd}"
        self.query_one("#prompt", Input).focus()
        self._mount(Welcome(self.agent.model.name, self.agent.cwd))
        self._apply_panel()
        # Shells finish on their own, with no event to say so.
        self.set_interval(2.0, self._tick)

    def watch_busy(self, busy: bool) -> None:
        self._refresh_sidebar()

    # -- status line + task list --------------------------------------------
    def _status_text(self) -> Text:
        custom = self.statusline.text()
        if custom is not None:
            line = Text.from_ansi(custom, no_wrap=True, overflow="ellipsis")
            line.pad_left(1)
            return line
        s = self.agent.session
        u = s.total_usage
        tok = u.prompt_tokens + u.completion_tokens
        tok_str = f"{tok / 1000:.1f}k" if tok >= 1000 else str(tok)
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("◆ ", style=ui.ACCENT)
        t.append(self.agent.model.name, style=f"bold {ui.ACCENT}")
        if self.busy:
            doing = ui.current_todo(self.agent.todos.items)
            done, total, _ = ui.todo_progress(self.agent.todos.items)
            if doing:
                t.append(f"  ·  {done}/{total} {doing}", style=ui.ACCENT_SOFT)
            else:
                t.append("  ·  working…", style=ui.ACCENT_SOFT)
        if self.agent.mode == "plan":
            t.append("  ·  plan mode (read-only)", style=f"bold {ui.WARN}")
        elif self.agent.permission_mode == "accept-edits":
            t.append("  ·  ⏵⏵ accept edits", style=f"bold {ui.ACCENT2}")
        t.append(f"  ·  {s.turns} turns  ·  {tok_str} tokens  ·  "
                 f"~${s.estimated_cost(self.agent.model):.4f}")
        if getattr(s, "compactions", 0):
            t.append(f"  ·  compacted {s.compactions}×")
        t.append("  ·  shift+tab mode  ^B panel  ^C quit  ^L clear  ^U undo")
        return t

    def _tasks_text(self) -> Text:
        t = Text()
        items = self.agent.todos.items
        if items:
            t.append(ui.todo_title(items) + "\n", style=f"bold {ui.ACCENT}")
        marks = {"pending": "☐", "in_progress": "◐", "completed": "☑"}
        for item in items:
            status = item.get("status", "pending")
            style = (f"bold {ui.ACCENT}" if status == "in_progress" else
                     f"{ui.MUTED} strike" if status == "completed" else "")
            t.append(f"{marks.get(status, '☐')} {item.get('content', '')}\n", style=style)
        t.rstrip()
        return t

    def _refresh_sidebar(self, files: bool = False) -> None:
        """Refresh the status line, the task list and the side panel.

        `files` recomputes the changed-file counts, which reads files from
        disk, so it is done after a tool result or a turn, not per token."""
        try:
            self.query_one("#statusline", Static).update(self._status_text())
            tasks = self.query_one("#tasks", Static)
            tasks.update(self._tasks_text())
            tasks.set_class(bool(self.agent.todos.items), "shown")
            if not self.panel_open:
                return
            if files:
                self._changes = sidebar.file_changes(self.agent.checkpoints, self.agent.cwd)
            self.query_one("#side-body", Static).update(sidebar.render(
                self.agent, busy=self.busy, queued=self._queued, changes=self._changes,
                tasks=False))
        except Exception:  # noqa: BLE001 - the panel must never break a turn
            pass

    # -- side panel -----------------------------------------------------------
    @property
    def panel_open(self) -> bool:
        if self._panel_choice is not None:
            return self._panel_choice
        return self.size.width >= sidebar.AUTO_OPEN_WIDTH

    def _apply_panel(self) -> None:
        try:
            self.query_one("#sidebar").set_class(self.panel_open, "open")
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        self._refresh_sidebar(files=True)

    def on_resize(self, event) -> None:
        self._apply_panel()

    def action_toggle_sidebar(self) -> None:
        self._panel_choice = not self.panel_open
        self._apply_panel()

    def _tick(self) -> None:
        if (self.panel_open and self.agent.shells.list()) or self.statusline.enabled:
            self._refresh_sidebar()

    # -- transcript helpers -------------------------------------------------
    def _mount(self, widget) -> None:
        log = self.query_one("#transcript", VerticalScroll)
        log.mount(widget)
        log.scroll_end(animate=False)

    def _finalize_assistant(self) -> None:
        self._assistant_md = None
        self._assistant_buf = ""
        self._thought = None
        self._thought_buf = ""

    # -- input --------------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if self.busy:
            # Don't swallow a question asked mid-turn: queue it and run it next.
            self._queued.append(text)
            self._mount(StatusNote(f"queued ({len(self._queued)}): {text}"))
            self._refresh_sidebar()
            return
        self._submit(text)

    def _submit(self, text: str) -> None:
        """Handle one message: slash commands first, then a turn."""
        if text in ("/exit", "/quit"):
            self._queued.clear()
            self.exit()
            return
        if text in ("/clear", "/reset"):
            self.action_clear()
            return
        if text == "/undo":
            self.action_undo()
            return
        if text.split()[0] == "/plan":
            self.agent.set_mode("default" if self.agent.mode == "plan" else "plan")
            text = text[len("/plan"):].strip()
            self._refresh_sidebar()
            if not text:
                return
        elif text.startswith("/") and text.split()[0][1:] in self.commands:
            name = text.split()[0][1:]
            text = self.commands[name].expand(text[len(name) + 1:])
        self._start_turn(text)

    @work(thread=True, exclusive=True)
    def _run_agent(self, text: str) -> None:
        self.notifier.start()
        try:
            self.notifier.finished(self.agent.run(text))
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._mount, StatusNote(explain_failure(exc)))
        finally:
            if self._on_turn:
                try:
                    self._on_turn()
                except Exception:  # noqa: BLE001
                    pass
            self.call_from_thread(self._turn_done)

    def _turn_done(self) -> None:
        self.busy = False
        self._finalize_assistant()
        self._refresh_sidebar(files=True)
        self.query_one("#prompt", Input).focus()
        # Queued text takes the same path as typed text, so a slash command
        # typed mid-turn still acts as a command, not as a prompt — and a
        # command that starts no turn does not stall the rest of the queue.
        while self._queued and not self.busy:
            self._submit(self._queued.pop(0))

    def _start_turn(self, text: str) -> None:
        self._mount(UserMessage(text))
        self.busy = True
        self._run_agent(text)

    # -- agent events (called from worker thread) ---------------------------
    def _agent_event(self, ev: dict) -> None:
        # Marshal onto the UI thread (or handle directly if already on it, e.g.
        # a status note from /plan).
        try:
            self.call_from_thread(self._handle_event, ev)
        except RuntimeError:
            self._handle_event(ev)

    def _handle_event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "assistant_start":
            self._finalize_assistant()
        elif kind == "assistant_delta":
            self._assistant_buf += ev.get("text", "")
            if self._assistant_md is None:
                self._assistant_md = Markdown("", classes="assistant")
                self._mount(self._assistant_md)
            self._assistant_md.update(self._assistant_buf)
            self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)
        elif kind == "assistant_end":
            self._finalize_assistant()
        elif kind == "reasoning_delta" and self.agent.config.show_reasoning:
            self._thought_buf += ev.get("text", "")
            if self._thought is None:
                self._thought = Static("", classes="statusnote")
                self._mount(self._thought)
            self._thought.update(Text(self._thought_buf, style=f"{ui.MUTED} italic"))
        elif kind == "todos":
            pass  # rendered in the sidebar (refreshed below)
        elif kind == "tool_call":
            self._finalize_assistant()
            self._mount(ToolCall(ev["name"], ev.get("args", {}), self.agent.cwd))
        elif kind == "tool_result":
            self._mount(ToolResult(ev.get("content", ""), ev.get("is_error", False),
                                   ev.get("name", ""), self.agent.cwd))
        elif kind == "diff":
            self._mount(DiffView(ev.get("diff", "")))
        elif kind == "status":
            self._mount(StatusNote(ev.get("text", "")))
        if kind in ("assistant_delta", "reasoning_delta"):
            # Per token: only the cheap status line, not the panel.
            try:
                self.query_one("#statusline", Static).update(self._status_text())
            except Exception:  # noqa: BLE001
                pass
            return
        self._refresh_sidebar(files=kind == "tool_result")

    # -- permission prompt (blocks the worker thread) -----------------------
    def _permission_prompt(self, title: str, detail: str) -> str:
        done = threading.Event()
        result = {"v": "no"}
        self.notifier.waiting(title)

        def ask():
            def cb(choice):
                result["v"] = choice or "no"
                done.set()
            self.push_screen(PermissionModal(title, detail), cb)

        self.call_from_thread(ask)
        done.wait()
        return result["v"]

    def _question_prompt(self, question: str, options: list) -> str:
        done = threading.Event()
        result = {"v": ""}
        self.notifier.waiting("Question")

        def ask():
            def cb(answer):
                result["v"] = answer or ""
                done.set()
            self.push_screen(QuestionModal(question, options), cb)

        self.call_from_thread(ask)
        done.wait()
        return result["v"]

    # -- actions ------------------------------------------------------------
    def action_clear(self) -> None:
        self.agent.session.reset()
        self.query_one("#transcript", VerticalScroll).remove_children()
        self._finalize_assistant()
        self._mount(StatusNote("Conversation cleared."))
        self._refresh_sidebar()

    def action_toggle_plan(self) -> None:
        if self.busy:
            return  # not while a turn is running
        self.agent.cycle_mode()  # normal → accept edits → plan
        self._refresh_sidebar()

    def action_undo(self) -> None:
        if self.agent.checkpoints:
            msg = self.agent.checkpoints.undo_last()
        else:
            msg = "Checkpoints disabled."
        self._mount(StatusNote(msg))
        self._refresh_sidebar(files=True)


def run_tui(agent: Agent, on_turn=None) -> None:
    CodedTUI(agent, on_turn=on_turn).run()
