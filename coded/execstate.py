"""What is running, kept apart from the input.

The input (the REPL prompt, the mid-turn box, the TUI's field) never waits on
execution, and execution never reads keys. Both front-ends keep one
`Execution` per session: they mark a request's start and end, a question
waiting for the user, and an interrupt; the rest is read live from the agent
(running tools, follow-ups not yet delivered). `state()` names the current
state and `badge()` draws it for the status line.

    IDLE                    nothing has run yet
    INPUT_READY             the prompt is up between requests
    COMMAND_RUNNING         a request (or a command that runs the agent) is working
    TOOL_EXECUTING          … and a tool call is running right now
    WAITING_FOR_USER_INPUT  … and a permission prompt / question waits for you
    FOLLOW_UP_PENDING       … and something you typed waits for the next step
    INTERRUPTING            you stopped it; it is finishing the current step
    COMPLETED / FAILED / INTERRUPTED   how the last request ended
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any, Callable, List, Optional, Tuple


class ExecState(str, Enum):
    IDLE = "idle"
    INPUT_READY = "input ready"
    COMMAND_RUNNING = "running"
    TOOL_EXECUTING = "running tool"
    WAITING_FOR_USER_INPUT = "waiting for you"
    FOLLOW_UP_PENDING = "follow-up pending"
    INTERRUPTING = "interrupting"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


RUNNING = {ExecState.COMMAND_RUNNING, ExecState.TOOL_EXECUTING,
           ExecState.WAITING_FOR_USER_INPUT, ExecState.FOLLOW_UP_PENDING,
           ExecState.INTERRUPTING}


class Execution:
    """One session's execution state. Thread-safe: the agent runs on one
    thread (REPL main / TUI worker), keys arrive on another, drawing on a third."""

    def __init__(self, agent: Any = None,
                 pending: Optional[Callable[[], int]] = None) -> None:
        self.agent = agent
        self._pending = pending or (lambda: 0)
        self._lock = threading.Lock()
        self._running: Optional[str] = None     # label of the request running
        self._started = 0.0
        self._waiting = 0                       # questions open (nested prompts)
        self._interrupting = False
        self._ready = False
        self.outcome: Optional[ExecState] = None
        self.last_label = ""
        self.last_seconds = 0.0
        self.last_error = ""
        self.history: List[Tuple[float, ExecState, str]] = []  # (time, state, label)

    # -- transitions (front-ends call these) -----------------------------------
    def begin(self, label: str) -> None:
        with self._lock:
            self._running = label
            self._started = time.monotonic()
            self._interrupting = False
            self._ready = False
            self._record(ExecState.COMMAND_RUNNING, label)

    def end(self, ok: bool = True, error: str = "") -> ExecState:
        with self._lock:
            if self._running is None:
                return self.outcome or ExecState.IDLE
            outcome = (ExecState.INTERRUPTED if self._interrupting else
                       ExecState.COMPLETED if ok else ExecState.FAILED)
            self.outcome = outcome
            self.last_label, self.last_error = self._running, error
            self.last_seconds = time.monotonic() - self._started
            self._running = None
            self._interrupting = False
            self._waiting = 0
            self._record(outcome, self.last_label)
            return outcome

    def ready(self) -> None:
        """The prompt is up for the next request."""
        with self._lock:
            self._ready = True

    def interrupt(self) -> None:
        with self._lock:
            if self._running is not None and not self._interrupting:
                self._interrupting = True
                self._record(ExecState.INTERRUPTING, self._running)

    def waiting(self) -> "_Waiting":
        """`with execution.waiting():` around a question put to the user."""
        return _Waiting(self)

    # -- reading ---------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running is not None

    def active_tools(self) -> List[str]:
        return list(getattr(self.agent, "active_tools", None) or [])

    def pending(self) -> int:
        try:
            return int(self._pending() or 0)
        except Exception:  # noqa: BLE001
            return 0

    def state(self) -> ExecState:
        with self._lock:
            running, waiting, stopping = self._running, self._waiting, self._interrupting
            ready, outcome = self._ready, self.outcome
        if running is None:
            if ready and outcome is None:
                return ExecState.INPUT_READY
            return outcome or ExecState.IDLE
        if stopping:
            return ExecState.INTERRUPTING
        if waiting:
            return ExecState.WAITING_FOR_USER_INPUT
        if self.active_tools():
            return ExecState.TOOL_EXECUTING
        if self.pending():
            return ExecState.FOLLOW_UP_PENDING
        return ExecState.COMMAND_RUNNING

    def detail(self) -> str:
        """What goes with the state: running tools, follow-ups waiting, time."""
        st = self.state()
        parts: List[str] = []
        if st in RUNNING:
            tools = self.active_tools()
            if tools:
                parts.append(", ".join(sorted(set(tools))))
            n = self.pending()
            if n and st is not ExecState.FOLLOW_UP_PENDING:
                parts.append(f"{n} follow-up{'s' if n > 1 else ''} waiting")
            elif n:
                parts.append(f"{n} message{'s' if n > 1 else ''} for the next step")
        elif st in (ExecState.COMPLETED, ExecState.FAILED, ExecState.INTERRUPTED):
            parts.append(f"{self.last_seconds:.0f}s")
            if self.last_error:
                parts.append(self.last_error[:60])
        return " · ".join(parts)

    def _record(self, state: ExecState, label: str) -> None:
        self.history.append((time.time(), state, label))
        del self.history[:-200]


class _Waiting:
    def __init__(self, ex: Execution) -> None:
        self.ex = ex

    def __enter__(self) -> None:
        with self.ex._lock:
            self.ex._waiting += 1
            if self.ex._running is not None:
                self.ex._record(ExecState.WAITING_FOR_USER_INPUT, self.ex._running)

    def __exit__(self, *exc) -> None:
        with self.ex._lock:
            self.ex._waiting = max(0, self.ex._waiting - 1)


# How each state is drawn: (mark, palette name).
_LOOK = {
    ExecState.IDLE: ("○", "MUTED"), ExecState.INPUT_READY: ("○", "MUTED"),
    ExecState.COMMAND_RUNNING: ("●", "ACCENT"), ExecState.TOOL_EXECUTING: ("◆", "ACCENT"),
    ExecState.WAITING_FOR_USER_INPUT: ("?", "WARN"),
    ExecState.FOLLOW_UP_PENDING: ("↪", "ACCENT2"), ExecState.INTERRUPTING: ("■", "WARN"),
    ExecState.COMPLETED: ("✓", "ACCENT2"), ExecState.FAILED: ("✗", "ERR"),
    ExecState.INTERRUPTED: ("■", "WARN"),
}


def badge(ex: Execution):
    """The state as a rich Text: mark + name + detail."""
    from rich.text import Text

    from coded import ui

    st = ex.state()
    mark, color = _LOOK[st]
    style = getattr(ui, color)
    t = Text()
    t.append(f"{mark} {st.value}", style=f"bold {style}")
    detail = ex.detail()
    if detail:
        t.append(f"  {detail}", style=ui.MUTED)
    return t
