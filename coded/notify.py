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


# How an outcome can reach you. "line" prints in the transcript; the others
# reach you when you are in another window.
CHANNELS = ("desktop", "voice", "bell", "line")


def parse_channels(value: Any) -> List[str]:
    """`notify` from the config or /notify: "bell", "desktop,voice", ["line"],
    "all", "off" (none). Unknown names are dropped; order is kept."""
    if value is None:
        return ["bell"]
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    names = [str(v).strip().lower() for v in value or []]
    if "all" in names:
        return list(CHANNELS)
    if not names or names == ["off"] or "none" in names:
        return []
    return [n for n in dict.fromkeys(names) if n in CHANNELS]


def _speakable(text: str) -> str:
    """Only plain words reach a speech command (no quotes, no shell syntax)."""
    import re

    return re.sub(r"[^A-Za-z0-9 .,:'-]+", " ", text).replace("'", "")[:160].strip()


def voice_command(text: str, platform: str,
                  which: Callable[[str], Optional[str]]) -> Optional[List[str]]:
    """The command that says `text` aloud here, or None."""
    words = _speakable(text)
    if not words:
        return None
    if platform == "darwin" and which("say"):
        return ["say", words]
    if platform.startswith("linux"):
        for tool in ("spd-say", "espeak", "espeak-ng"):
            if which(tool):
                return [tool, words]
    if platform.startswith("win"):
        shell = which("powershell") or which("pwsh")
        if shell:
            return [shell, "-NoProfile", "-Command",
                    "Add-Type -AssemblyName System.Speech; "
                    "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak("
                    f"'{words}')"]
    return None


def speak(text: str, *, platform: Optional[str] = None,
          which: Optional[Callable[[str], Optional[str]]] = None,
          spawn: Optional[Callable[..., Any]] = None) -> bool:
    argv = voice_command(text, platform or sys.platform, which or shutil.which)
    if not argv:
        return False
    try:
        (spawn or subprocess.Popen)(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001 - speech is best effort
        return False


def _line(kind: str, text: str) -> None:
    from coded import ui

    ui.gap()
    style = ui.ACCENT2 if kind == "completed" else ui.WARN
    ui.console.print(f"[bold {style}]{'✓' if kind == 'completed' else '■'}[/] "
                     f"[{style}]{_esc(text)}[/]")


def _esc(text: str) -> str:
    from rich.markup import escape

    return escape(text)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def outcome_of(agent: Any, ok: bool, error: str = "", interrupted: bool = False):
    """("completed" | "stopped", reason) for the request that just ended, read
    from how it ended and the event log of that request."""
    if interrupted:
        return "stopped", "you stopped it"
    if not ok:
        return "stopped", "error: " + (error.splitlines()[0][:100] if error else "it failed")
    events = []
    try:
        items = agent.events.snapshot()
        for i in range(len(items) - 1, -1, -1):
            if items[i].get("kind") == "turn":
                events = items[i:]
                break
    except Exception:  # noqa: BLE001 - no log: judge by ok alone
        events = []
    if any(e.get("kind") == "interrupt" for e in events):
        return "stopped", "you stopped it"
    stops = [e for e in events if e.get("kind") == "max_turns"]
    if stops:
        why = str(stops[-1].get("reason") or "")
        if "progress" in why:
            return "stopped", "it was not making progress"
        return "stopped", "it reached the step limit"
    try:
        if agent.over_budget():
            return "stopped", "the spending limit was reached"
    except Exception:  # noqa: BLE001
        pass
    return "completed", ""


class Notifier:
    """Times a request and tells you how it ended (or that it waits for you),
    if it ran at least `after` seconds — through the chosen channels."""

    def __init__(self, mode: Any = "bell", after: float = 30.0,
                 clock: Callable[[], float] = time.monotonic, sender=send,
                 speaker: Callable[[str], Any] = speak,
                 liner: Callable[[str, str], Any] = _line):
        self.channels = parse_channels(mode)
        self.enabled = bool(self.channels)
        if not self.enabled:
            self.channels = ["bell"]  # what /notify on brings back
        self.after = float(after)
        self._clock = clock
        self._send = sender
        self._speak = speaker
        self._line = liner
        self._started: Optional[float] = None

    @property
    def mode(self) -> str:
        """The old single-mode view: "desktop", "bell" or "off"."""
        if not self.enabled:
            return "off"
        return "desktop" if "desktop" in self.channels else "bell"

    @classmethod
    def from_config(cls, config: Any) -> "Notifier":
        return cls(getattr(config, "notify", "bell"), getattr(config, "notify_after", 30.0))

    # -- the switch ---------------------------------------------------------------
    def set(self, words: List[str]) -> str:
        """/notify on|off|all|<channels>: change it for this session; the new state."""
        words = [w.lower() for w in words]
        if words == ["on"]:
            self.enabled = True
        elif words == ["off"]:
            self.enabled = False
        elif words:
            picked = parse_channels(words)
            if picked:
                self.channels, self.enabled = picked, True
        return self.describe()

    def describe(self) -> str:
        if not self.enabled:
            return "Notifications are off (/notify on turns them back on)."
        return (f"Notifications on: {', '.join(self.channels)} — when work that ran "
                f"{self.after:g}s or longer completes or stops.")

    # -- events ----------------------------------------------------------------------
    def start(self) -> None:
        self._started = self._clock()

    def _elapsed(self) -> Optional[float]:
        return None if self._started is None else self._clock() - self._started

    def _long(self) -> bool:
        elapsed = self._elapsed()
        return elapsed is not None and elapsed >= self.after

    def _alert(self, title: str, message: str, spoken: str) -> List[str]:
        used: List[str] = []
        if "desktop" in self.channels:
            used.append(self._send(title, message, "desktop"))
        if "bell" in self.channels and "bell" not in used:
            used.append(self._send(title, message, "bell"))
        if "voice" in self.channels and self._speak(spoken):
            used.append("voice")
        return used

    def waiting(self, question: str) -> Optional[str]:
        """A question is on screen and the request has run long: call you over."""
        if not self.enabled or not self._long():
            return None
        used = self._alert("coded needs you", question, "coded needs your answer")
        return used[0] if used else None

    def outcome(self, kind: str, reason: str = "", label: str = "",
                force: bool = False) -> List[str]:
        """The request ended: "completed", or "stopped" with why. Quiet for
        short requests unless `force` (missions). Returns the channels used."""
        elapsed = self._elapsed()
        self._started = None
        if not self.enabled or (not force and (elapsed is None or elapsed < self.after)):
            return []
        took = f" ({_duration(elapsed)})" if elapsed is not None else ""
        what = " ".join((label or "").split())
        what = what[:70] + ("…" if len(what) > 70 else "")
        if kind == "completed":
            head, spoken = "Task completed", "Task completed"
        else:
            head = "Task stopped" + (f": {reason}" if reason else "")
            spoken = "Task stopped" + (f". {reason}" if reason else "")
        used: List[str] = []
        if "line" in self.channels:
            self._line(kind, head + took + (f" — {what}" if what else ""))
            used.append("line")
        used += self._alert(f"coded: {head}", what or head, spoken)
        return used

    def finished(self, summary: str = "") -> Optional[str]:
        """Older callers: the request completed."""
        used = self.outcome("completed", label=summary)
        return used[0] if used else None
