"""A full-screen terminal UI (Textual) for coded, à la Claude Code.

Layout:
  ┌ header ────────────────────────────────────────────┐
  │ conversation transcript (scrollable) │  session     │
  │  · user / assistant / tool cards     │  sidebar      │
  ├──────────────────────────────────────┴──────────────┤
  │ input                                                │
  └ footer (key hints) ──────────────────────────────────┘

The synchronous agent runs in a worker thread; it emits structured events
(via Agent.on_event) that are marshalled onto the UI thread to build widgets.
Permission requests open a modal and block the worker until answered.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from rich.markup import escape as _esc
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    Static,
)

from coded.agent import Agent


# ---------------------------------------------------------------------------
# Widgets for the transcript
# ---------------------------------------------------------------------------
class UserMessage(Static):
    def __init__(self, text: str):
        super().__init__(Text(text, style="bold"), classes="msg user")


class ToolCall(Static):
    def __init__(self, name: str, args: dict):
        import json
        try:
            arg_str = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            arg_str = str(args)
        if len(arg_str) > 240:
            arg_str = arg_str[:240] + "…"
        body = Text()
        body.append("● ", style="#7c9cff")
        body.append(name, style="bold #7c9cff")
        body.append("  " + arg_str, style="#8a94a7")
        super().__init__(body, classes="toolcall")


class ToolResult(Static):
    def __init__(self, content: str, is_error: bool):
        lines = content.splitlines() or [content]
        shown = lines[:14]
        style = "#f7768e" if is_error else "#8a94a7"
        body = Text()
        for line in shown:
            body.append(line + "\n", style=style)
        if len(lines) > len(shown):
            body.append(f"… (+{len(lines) - len(shown)} more)\n", style="#8a94a7 italic")
        cls = "toolresult error" if is_error else "toolresult"
        super().__init__(body, classes=cls)


class DiffView(Static):
    def __init__(self, diff: str):
        super().__init__(Syntax(diff, "diff", theme="ansi_dark", word_wrap=False),
                         classes="diff")


class StatusNote(Static):
    def __init__(self, text: str):
        super().__init__(Text("• " + text, style="#8a94a7 italic"), classes="statusnote")


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
            yield Label(f"[b yellow]{_esc(self._title)}[/]", id="perm-title")
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


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------
class CodedTUI(App):
    CSS = """
    Screen { background: #0b0e14; }
    #main { height: 1fr; }
    #transcript { width: 3fr; padding: 0 1; }
    #sidebar { width: 1fr; border-left: solid #232838; padding: 1; color: #8a94a7; }
    #sidebar .h { color: #7c9cff; text-style: bold; }
    .msg.user { background: #161c2c; color: #e6ebf5; padding: 0 1; margin: 1 0 0 0; border-left: thick #7c9cff; }
    .assistant { padding: 0 1; margin: 0 0 1 0; }
    .toolcall { margin: 1 0 0 0; }
    .toolresult { color: #8a94a7; padding: 0 0 0 2; }
    .toolresult.error { color: #f7768e; }
    .diff { margin: 0 0 1 2; }
    .statusnote { margin: 0 0 0 1; }
    #prompt { border: round #232838; }
    #prompt:focus { border: round #7c9cff; }
    #perm-box { background: #11151f; border: round #e0af68; padding: 1 2; width: 70; height: auto; align: center middle; }
    #perm-buttons { height: auto; align: center middle; padding-top: 1; }
    PermissionModal { align: center middle; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+l", "clear", "Clear"),
        Binding("ctrl+u", "undo", "Undo edits"),
    ]

    busy = reactive(False)

    def __init__(self, agent: Agent, on_turn=None):
        super().__init__()
        self.agent = agent
        self._on_turn = on_turn
        self.agent.on_event = self._agent_event  # called from the worker thread
        self.agent.permissions.set_prompt(self._permission_prompt)
        self._assistant_md: Optional[Markdown] = None
        self._assistant_buf = ""

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield VerticalScroll(id="transcript")
            yield Static(self._sidebar_text(), id="sidebar")
        yield Input(placeholder="Ask coded to build, fix, explain…  (Enter to send)", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "coded"
        self.sub_title = f"{self.agent.model.name}  ·  {self.agent.cwd}"
        self.query_one("#prompt", Input).focus()
        self._mount(StatusNote("Ready. Type a request and press Enter."))

    # -- sidebar ------------------------------------------------------------
    def _sidebar_text(self) -> Text:
        s = self.agent.session
        u = s.total_usage
        tok = u.prompt_tokens + u.completion_tokens
        cost = s.estimated_cost(self.agent.model)
        t = Text()
        t.append("SESSION\n", style="#7c9cff bold")
        t.append(f"model   {self.agent.model.name}\n")
        t.append(f"turns   {s.turns}\n")
        t.append(f"tokens  {tok:,}\n")
        t.append(f"cost    ~${cost:.4f}\n")
        if getattr(s, "compactions", 0):
            t.append(f"compacted {s.compactions}×\n")
        t.append("\nTOOLS\n", style="#7c9cff bold")
        t.append(", ".join(self.agent.registry.names()) + "\n", style="#8a94a7")
        if self.agent.skills:
            t.append("\nSKILLS\n", style="#7c9cff bold")
            t.append(", ".join(self.agent.skills) + "\n", style="#8a94a7")
        return t

    def _refresh_sidebar(self) -> None:
        try:
            self.query_one("#sidebar", Static).update(self._sidebar_text())
        except Exception:  # noqa: BLE001
            pass

    # -- transcript helpers -------------------------------------------------
    def _mount(self, widget) -> None:
        log = self.query_one("#transcript", VerticalScroll)
        log.mount(widget)
        log.scroll_end(animate=False)

    def _finalize_assistant(self) -> None:
        self._assistant_md = None
        self._assistant_buf = ""

    # -- input --------------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self.busy:
            return
        event.input.value = ""
        if text in ("/exit", "/quit"):
            self.exit()
            return
        if text in ("/clear", "/reset"):
            self.action_clear()
            return
        if text == "/undo":
            self.action_undo()
            return
        self._mount(UserMessage(text))
        self.busy = True
        self._run_agent(text)

    @work(thread=True, exclusive=True)
    def _run_agent(self, text: str) -> None:
        try:
            self.agent.run(text)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._mount, StatusNote(f"error: {exc}"))
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
        self._refresh_sidebar()
        self.query_one("#prompt", Input).focus()

    # -- agent events (called from worker thread) ---------------------------
    def _agent_event(self, ev: dict) -> None:
        # Marshal onto the UI thread.
        self.call_from_thread(self._handle_event, ev)

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
        elif kind == "tool_call":
            self._finalize_assistant()
            self._mount(ToolCall(ev["name"], ev.get("args", {})))
        elif kind == "tool_result":
            self._mount(ToolResult(ev.get("content", ""), ev.get("is_error", False)))
        elif kind == "diff":
            self._mount(DiffView(ev.get("diff", "")))
        elif kind == "status":
            self._mount(StatusNote(ev.get("text", "")))
        self._refresh_sidebar()

    # -- permission prompt (blocks the worker thread) -----------------------
    def _permission_prompt(self, title: str, detail: str) -> str:
        done = threading.Event()
        result = {"v": "no"}

        def ask():
            def cb(choice):
                result["v"] = choice or "no"
                done.set()
            self.push_screen(PermissionModal(title, detail), cb)

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

    def action_undo(self) -> None:
        if self.agent.checkpoints:
            msg = self.agent.checkpoints.undo_last()
        else:
            msg = "Checkpoints disabled."
        self._mount(StatusNote(msg))


def run_tui(agent: Agent, on_turn=None) -> None:
    CodedTUI(agent, on_turn=on_turn).run()
