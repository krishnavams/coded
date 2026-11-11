"""Typing while the agent works.

A turn can run for minutes, and until now the terminal was dead for all of it:
a question that occurred to you mid-turn had to wait, and the permission prompt
was the only thing that could read a key.

`TypeAhead` owns stdin for the duration of a turn. Lines typed while the agent
works are queued and run as the next messages; when the agent needs an answer
(the permission prompt), `ask()` takes the next line instead, so the two never
race for the same keystrokes.

Reading is polled, never blocking, so the reader thread stops the moment the
turn ends and never steals the first line typed at the next prompt. The poll is
injectable, which is also how the tests drive it without a terminal.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from typing import Callable, List, Optional

POLL_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Platform readers: return a complete line, or None if nothing was typed.
# ---------------------------------------------------------------------------
def _posix_poll(timeout: float) -> Optional[str]:
    import select

    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    line = sys.stdin.readline()
    return None if line == "" else line.rstrip("\r\n")


class _WindowsPoll:
    """Console input on Windows, where stdin cannot be select()ed.

    Characters are collected (and echoed) as they are typed, and handed over as
    a line on Enter. A line still being typed when the turn ends stays in `buf`
    and is handed to the next prompt by `TypeAhead.partial`.
    """

    def __init__(self) -> None:
        self.buf: List[str] = []

    def __call__(self, timeout: float) -> Optional[str]:
        import msvcrt
        import time

        deadline = time.monotonic() + timeout
        while True:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    line, self.buf = "".join(self.buf), []
                    return line
                if ch == "\x08":  # backspace
                    if self.buf:
                        self.buf.pop()
                        sys.stdout.write("\b \b")
                        sys.stdout.flush()
                    continue
                if ch == "\x03":  # Ctrl-C: the console signals the process itself
                    self.buf = []
                    return None
                if ch in ("\x00", "\xe0"):  # a function/arrow key: drop the pair
                    msvcrt.getwch()
                    continue
                self.buf.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)


def default_poll() -> Optional[Callable[[float], Optional[str]]]:
    """The reader for this terminal, or None if type-ahead cannot work here."""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return None
    except Exception:  # noqa: BLE001 - a closed or odd stdin
        return None
    if os.name == "nt":
        try:
            import msvcrt  # noqa: F401
        except ImportError:
            return None
        return _WindowsPoll()
    try:
        import select  # noqa: F401
    except ImportError:
        return None
    return _posix_poll


# ---------------------------------------------------------------------------
class TypeAhead:
    """Collects lines typed while a turn runs; routes one to a waiting prompt."""

    def __init__(self, poll: Optional[Callable[[float], Optional[str]]] = None,
                 on_queue: Optional[Callable[[str, int], None]] = None):
        self._poll = poll
        self._on_queue = on_queue
        self._queued: "queue.Queue[str]" = queue.Queue()
        self._answers: "queue.Queue[str]" = queue.Queue()
        self._want_answer = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def available(self) -> bool:
        return self._poll is not None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._poll is None or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def __enter__(self) -> "TypeAhead":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- the reader ---------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                line = self._poll(POLL_SECONDS)  # type: ignore[misc]
            except Exception:  # noqa: BLE001 - a dead terminal just ends type-ahead
                return
            if line is None:
                continue
            self.feed(line)

    def feed(self, line: str) -> None:
        """Deliver one typed line (also the test entry point)."""
        if self._want_answer.is_set():
            self._want_answer.clear()
            self._answers.put(line)
            return
        text = line.strip()
        if not text:
            return
        self._queued.put(text)
        if self._on_queue:
            self._on_queue(text, self._queued.qsize())

    # -- consumers ----------------------------------------------------------
    def ask(self, timeout: Optional[float] = None) -> Optional[str]:
        """Take the next typed line for a prompt rather than the queue.

        Anything typed *before* the question was asked stays queued: it was
        meant for the agent, not as an answer to a question not yet on screen.
        """
        if self._poll is None and self._answers.empty():
            return None
        self._want_answer.set()
        try:
            return self._answers.get(timeout=timeout) if timeout else self._answers.get()
        except queue.Empty:
            return None
        finally:
            self._want_answer.clear()

    def requeue(self, text: str) -> None:
        """Put a line back: it was typed as a message, not as an answer."""
        if text.strip():
            self._queued.put(text.strip())

    def drain(self) -> List[str]:
        out: List[str] = []
        while True:
            try:
                out.append(self._queued.get_nowait())
            except queue.Empty:
                return out

    @property
    def pending(self) -> int:
        return self._queued.qsize()

    @property
    def partial(self) -> str:
        """A line the user was still typing when the turn ended (Windows only;
        on POSIX the terminal keeps it until Enter). The caller pre-fills the
        next prompt with it so nothing typed is lost."""
        buf = getattr(self._poll, "buf", None)
        if not buf:
            return ""
        text, buf[:] = "".join(buf), []
        return text
