"""Tell the user when a long turn finishes or stops to ask something.

A turn can run for minutes; people switch windows and miss the permission
question or the answer. `Notifier` rings the terminal bell or shows a desktop
notification, but only for turns that have run at least `notify_after`
seconds, so quick exchanges stay quiet.

Config: `"notify": "bell" | "desktop" | "off"` and `"notify_after": 30`.
Desktop notifications use osascript (macOS) or notify-send (Linux); anywhere
else, or when the tool is missing, they fall back to the bell.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from typing import Any, Callable, List, Optional

MODES = ("bell", "desktop", "off")


def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def desktop_command(title: str, message: str, platform: str,
                    which: Callable[[str], Optional[str]]) -> Optional[List[str]]:
    """The command that shows a desktop notification here, or None."""
    if platform == "darwin" and which("osascript"):
        return ["osascript", "-e",
                f"display notification {_applescript_quote(message)} "
                f"with title {_applescript_quote(title)}"]
    if platform.startswith("linux") and which("notify-send"):
        return ["notify-send", "--app-name=coded", title, message]
    return None


def send(title: str, message: str, mode: str = "bell", *, platform: Optional[str] = None,
         which: Optional[Callable[[str], Optional[str]]] = None,
         spawn: Optional[Callable[..., Any]] = None, out: Any = None) -> str:
    """Notify once; returns what was done: "bell", "desktop" or "off"."""
    if mode == "off":
        return "off"
    if mode == "desktop":
        argv = desktop_command(title, message, platform or sys.platform, which or shutil.which)
        if argv:
            try:
                (spawn or subprocess.Popen)(argv, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
                return "desktop"
            except Exception:  # noqa: BLE001 - fall back to the bell
                pass
    stream = out or sys.stdout
    try:
        stream.write("\a")
        stream.flush()
    except Exception:  # noqa: BLE001 - a closed stdout: nothing to ring
        return "off"
    return "bell"


class Notifier:
    """Times a turn and notifies when it ends or waits, if it ran long enough."""

    def __init__(self, mode: str = "bell", after: float = 30.0,
                 clock: Callable[[], float] = time.monotonic, sender=send):
        self.mode = mode if mode in MODES else "bell"
        self.after = float(after)
        self._clock = clock
        self._send = sender
        self._started: Optional[float] = None

    @classmethod
    def from_config(cls, config: Any) -> "Notifier":
        return cls(getattr(config, "notify", "bell"), getattr(config, "notify_after", 30.0))

    def start(self) -> None:
        self._started = self._clock()

    def _long(self) -> bool:
        return self._started is not None and self._clock() - self._started >= self.after

    def waiting(self, question: str) -> Optional[str]:
        """A permission question is on screen."""
        if self.mode == "off" or not self._long():
            return None
        return self._send("coded needs you", question, self.mode)

    def finished(self, summary: str = "") -> Optional[str]:
        """The turn ended."""
        long_enough = self._long()
        self._started = None
        if self.mode == "off" or not long_enough:
            return None
        text = " ".join((summary or "The turn is done.").split())
        return self._send("coded finished", text[:120], self.mode)
