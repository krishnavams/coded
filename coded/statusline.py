"""A user-defined status line: your script decides what the bar shows.

    "statusline": {"command": "~/bin/coded-status", "refresh": 5}

The command runs through the shell with a JSON object on stdin — model, cwd,
mode, turns, tokens, cost, the current task — and the first line it prints
(ANSI colors allowed) replaces coded's status line in the REPL and the TUI.
It runs in the background at most every `refresh` seconds, so a slow script
never delays typing; until it has answered (or if it fails) the built-in
status line is shown.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Optional

DEFAULT_REFRESH = 5.0
TIMEOUT = 5.0


def status_data(agent: Any) -> Dict[str, Any]:
    from coded import ui

    s = agent.session
    u = s.total_usage
    items = list(getattr(agent.todos, "items", []) or [])
    done, total, _ = ui.todo_progress(items)
    return {
        "model": agent.model.name,
        "cwd": agent.cwd,
        "mode": agent.permission_mode,
        "turns": s.turns,
        "input_tokens": u.prompt_tokens,
        "output_tokens": u.completion_tokens,
        "cost": round(s.estimated_cost(agent.model), 6),
        "tasks": {"done": done, "total": total, "current": ui.current_todo(items)},
    }


class StatusLine:
    def __init__(self, settings: Optional[Dict[str, Any]], data: Callable[[], Dict[str, Any]],
                 run: Optional[Callable[..., Any]] = None, clock=time.monotonic,
                 background: bool = True, on_change: Optional[Callable[[], None]] = None):
        settings = settings or {}
        self.command = str(settings.get("command") or "").strip()
        self.refresh = float(settings.get("refresh", DEFAULT_REFRESH))
        self._data = data
        self._run = run
        self._clock = clock
        self._background = background
        self._on_change = on_change
        self._text: Optional[str] = None
        self._last = float("-inf")
        self._busy = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    def _update(self) -> None:
        try:
            from coded import shells

            run = self._run or shells.run
            res = run(self.command, input=json.dumps(self._data()), timeout=TIMEOUT)
            out = (getattr(res, "stdout", "") or "").splitlines()
            text = out[0].rstrip() if out and getattr(res, "returncode", 0) == 0 else None
        except Exception:  # noqa: BLE001 - a broken script falls back to the built-in line
            text = None
        with self._lock:
            changed = text != self._text
            self._text = text
            self._busy = False
        if changed and self._on_change is not None:
            try:
                self._on_change()  # redraw the bar with the new line
            except Exception:  # noqa: BLE001
                pass

    def text(self) -> Optional[str]:
        """The latest line (None: show the built-in status line). Starts a
        refresh when the last one is older than `refresh` seconds."""
        if not self.enabled:
            return None
        now = self._clock()
        with self._lock:
            stale = now - self._last >= self.refresh and not self._busy
            if stale:
                self._last = now
                self._busy = True
            text = self._text
        if stale:
            if self._background:
                threading.Thread(target=self._update, daemon=True).start()
            else:
                self._update()
                with self._lock:
                    text = self._text
        return text
