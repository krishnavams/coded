"""Running sub-agents ("tasks"), so the front-end can show and control them.

Every sub-agent the main agent starts through the `task` tool is a `TaskEntry`
here: its label, model, running time, tool uses and latest activity. The REPL
draws the running ones at the bottom of the screen and lists them with
`/tasks`; a task can run in the background (`run_in_background`, or Ctrl+B
while it runs), in which case its report is handed back to the main agent as a
message when it finishes, while the user keeps chatting.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

# Activity lines kept per task (the dock shows the last few, or all when expanded).
_ACTIVITY = 30


@dataclass
class TaskEntry:
    id: str
    label: str
    model: str = ""
    background: bool = False
    status: str = "running"  # running | completed | failed | stopped
    started: float = field(default_factory=time.monotonic)
    finished: Optional[float] = None
    tool_uses: int = 0
    activity: Deque[str] = field(default_factory=lambda: deque(maxlen=_ACTIVITY))
    result: str = ""
    delivered: bool = False
    agent: Any = None  # the sub-agent, so it can be stopped
    # Set (by Ctrl+B) to move a foreground task to the background.
    detach: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def running(self) -> bool:
        return self.status == "running"

    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    def summary(self) -> str:
        """"7 tool uses · 1m 12s" — the same everywhere it is shown."""
        uses = f"{self.tool_uses} tool use{'s' if self.tool_uses != 1 else ''}"
        return f"{uses} · {format_elapsed(self.elapsed())}"


def format_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


class TaskManager:
    """The main agent's sub-agents, thread-safe (background tasks run on threads)."""

    def __init__(self) -> None:
        self._tasks: Dict[str, TaskEntry] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        # Called (on the task's thread) when a background task finishes; the
        # REPL uses it to wake the agent while the user is idle at the prompt.
        self.on_finish: Optional[Callable[[TaskEntry], None]] = None
        # True when a front-end can take a running task into the background
        # (Ctrl+B) and wake the agent with its result later.
        self.can_detach = False

    def start(self, label: str, model: str = "", background: bool = False) -> TaskEntry:
        with self._lock:
            entry = TaskEntry(id=f"t{next(self._ids)}", label=label or "task", model=model,
                              background=background)
            self._tasks[entry.id] = entry
        return entry

    def record(self, entry: TaskEntry, line: str) -> None:
        with self._lock:
            entry.tool_uses += 1
            entry.activity.append(line)

    def finish(self, entry: TaskEntry, result: str, status: str = "completed") -> None:
        with self._lock:
            if entry.status == "stopped" and status == "completed":
                status = "stopped"
            entry.status = status
            entry.result = result
            entry.finished = time.monotonic()
            entry.agent = None
            notify = entry.background
        entry.done.set()
        if notify and self.on_finish is not None:
            try:
                self.on_finish(entry)
            except Exception:  # noqa: BLE001 - a display hook must not lose the result
                pass

    def stop(self, task_id: str) -> bool:
        """Ask a running task to stop at its next step."""
        entry = self.get(task_id)
        if entry is None or not entry.running:
            return False
        agent = entry.agent
        entry.status = "stopped"
        if agent is not None:
            agent.interrupt()
        return True

    def get(self, task_id: str) -> Optional[TaskEntry]:
        return self._tasks.get(task_id)

    def all(self) -> List[TaskEntry]:
        with self._lock:
            return list(self._tasks.values())

    def running(self) -> List[TaskEntry]:
        return [t for t in self.all() if t.running]

    def foreground(self) -> Optional[TaskEntry]:
        """The task the main agent is waiting on right now, if any."""
        for t in self.running():
            if not t.background:
                return t
        return None

    def take_finished(self) -> List[TaskEntry]:
        """Background tasks whose report has not reached the agent yet (marks them)."""
        with self._lock:
            out = [t for t in self._tasks.values()
                   if t.background and not t.running and not t.delivered]
            for t in out:
                t.delivered = True
        return out

    def has_undelivered(self) -> bool:
        return any(t.background and not t.running and not t.delivered for t in self.all())


def report_message(entries: List[TaskEntry]) -> str:
    """The message that hands finished background tasks' reports to the agent."""
    parts = []
    for t in entries:
        head = f"[background task {t.id} \"{t.label}\" {t.status} — {t.summary()}]"
        parts.append(f"{head}\n{t.result or '(no output)'}")
    return "\n\n".join(parts)
