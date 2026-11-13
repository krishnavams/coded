"""Typing while the agent works.

A turn can run for minutes, and until now the terminal was dead for all of it:
a question that occurred to you mid-turn had to wait, and the permission prompt
was the only thing that could read a key.

`TypeAhead` owns stdin for the duration of a turn. Lines typed while the agent
works are queued and run as the next messages; when the agent needs an answer
(the permission prompt), `ask()` takes the next line instead, so the two never
race for the same keystrokes. A lone Esc stops the turn (`on_escape`), or
answers a waiting prompt with "no".

Reading is polled, never blocking, so the reader thread stops the moment the
turn ends and never steals the first line typed at the next prompt. The poll is
injectable, which is also how the tests drive it without a terminal.
"""

from __future__ import annotations

import os
import queue
from collections import deque
import sys
import threading
from typing import Callable, Dict, List, Optional

POLL_SECONDS = 0.05
# Returned by a poll when Esc is pressed on its own (not as part of an arrow
# key's escape sequence): the user wants the running turn stopped.
ESCAPE = "\x1b"
# Control keys handed to the front-end mid-turn (TypeAhead's `keys`):
# Ctrl+B moves the running sub-agent to the background, Ctrl+O expands or
# collapses tool output, Ctrl+T lists tasks.
CTRL_B, CTRL_O, CTRL_T = "\x02", "\x0f", "\x14"
# Up arrow: pull the newest queued message back into the input to edit it.
UP = "\x00up"
# Shift+Tab: turn accept-edits on or off while the turn runs.
SHIFT_TAB = "\x00stab"
CONTROL_KEYS = (CTRL_B, CTRL_O, CTRL_T, UP, SHIFT_TAB)
# Key mode (`TypeAhead.keys()`, for the choice prompts): the reader returns one
# key at a time as KEY + a name from `coded.choice` ("up", "enter", …) or the
# typed character itself.
KEY = "\x00key:"
_CSI_KEYS = {"[A": "up", "OA": "up", "[B": "down", "OB": "down", "[C": "right",
             "OC": "right", "[D": "left", "OD": "left", "[Z": "shift_tab"}
_WIN_KEYS = {"H": "up", "P": "down", "M": "right", "K": "left"}


def _key_name(ch: str) -> Optional[str]:
    """A single typed character as a key name (None: ignore it)."""
    if ch in ("\r", "\n"):
        return "enter"
    if ch in ("\x7f", "\x08"):
        return "backspace"
    if ch == "\t":
        return "tab"
    if ch == "\x15":
        return "ctrl_u"
    if ch < " ":
        return None
    return ch


# ---------------------------------------------------------------------------
# Platform readers: return a complete line, or None if nothing was typed.
# ---------------------------------------------------------------------------
class _PosixKeys:
    """Key-by-key reading on a POSIX terminal, so a bare Esc can stop a turn.

    The terminal is put in non-canonical, no-echo mode for the turn (signals
    stay on, so Ctrl-C still works); characters are echoed and collected here,
    Backspace and Ctrl-U edit the line, and Enter hands it over. An escape
    sequence (arrow keys, function keys) arrives as several bytes at once and is
    dropped; a lone Esc is returned as `ESCAPE`. A line still being typed when
    the turn ends stays in `buf` for the next prompt. `close()` restores the
    terminal (TypeAhead.stop calls it).
    """

    def __init__(self, fd: Optional[int] = None, out=None) -> None:
        import codecs

        self.fd = sys.stdin.fileno() if fd is None else fd
        self.out = out or sys.stdout
        self.buf: List[str] = []
        self.echo = True  # off while ui.TurnDock draws the line in its input box
        self.keys_mode = False  # one key per call (choice prompts), see KEY
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._saved = None
        self._pending = ""

    def _raw(self) -> None:
        if self._saved is not None:
            return
        import termios

        self._saved = termios.tcgetattr(self.fd)
        attrs = termios.tcgetattr(self.fd)
        # IEXTEN off too: otherwise the terminal itself takes ^O (discard
        # output) and, on macOS, ^T (status), and coded never sees them.
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.IEXTEN)
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def close(self) -> None:
        if self._saved is None:
            return
        import termios

        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self._saved)
        finally:
            self._saved = None

    def _echo(self, text: str) -> None:
        if not self.echo:
            return
        try:
            self.out.write(text)
            self.out.flush()
        except Exception:  # noqa: BLE001 - echo is cosmetic
            pass

    def _read(self, timeout: float) -> str:
        import select

        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return ""
        data = os.read(self.fd, 1024)
        if not data:
            raise EOFError
        return self._decoder.decode(data)

    def _key(self, timeout: float) -> Optional[str]:
        chunk = self._pending or self._read(timeout)
        self._pending = ""
        while chunk:
            ch, chunk = chunk[0], chunk[1:]
            if ch == "\x1b":
                rest = chunk or self._read(0.03)
                if not rest:
                    return KEY + "escape"
                j = 1
                if rest[:1] in ("[", "O"):
                    while j < len(rest) and not ("@" <= rest[j] <= "~"):
                        j += 1
                    j += 1
                self._pending = rest[j:]
                name = _CSI_KEYS.get(rest[:j])
                if name:
                    return KEY + name
                chunk, self._pending = self._pending, ""
                continue
            name = _key_name(ch)
            if name is not None:
                self._pending = chunk
                return KEY + name
        return None

    def __call__(self, timeout: float) -> Optional[str]:
        self._raw()
        if self.keys_mode:
            return self._key(timeout)
        chunk = self._pending or self._read(timeout)
        self._pending = ""
        i = 0
        while i < len(chunk):
            ch = chunk[i]
            i += 1
            if ch == "\x1b":
                rest = chunk[i:] or self._read(0.03)
                if not rest:
                    self._pending = ""
                    return ESCAPE
                # An escape sequence: skip it (CSI ends at a byte in @..~),
                # except the Up arrow, which edits the newest queued message.
                j = 0
                if rest[:1] in ("[", "O"):
                    j = 1
                    while j < len(rest) and not ("@" <= rest[j] <= "~"):
                        j += 1
                    j += 1
                else:
                    j = 1
                if rest[:j] in ("[A", "OA"):
                    self._pending = rest[j:]
                    return UP
                if rest[:j] == "[Z":
                    self._pending = rest[j:]
                    return SHIFT_TAB
                chunk, i = rest[j:], 0
                continue
            if ch in ("\r", "\n"):
                self._echo("\n")
                line, self.buf = "".join(self.buf), []
                self._pending = chunk[i:]
                return line
            if ch in ("\x7f", "\x08"):
                if self.buf:
                    self.buf.pop()
                    self._echo("\b \b")
                continue
            if ch == "\x15":  # Ctrl-U: clear the line
                self._echo("\b \b" * len(self.buf))
                self.buf = []
                continue
            if ch in CONTROL_KEYS:
                self._pending = chunk[i:]
                return ch
            if ch < " " and ch != "\t":
                continue
            self.buf.append(ch)
            self._echo(ch)
        return None


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
        self.echo = True  # off while ui.TurnDock draws the line in its input box
        self.keys_mode = False  # one key per call (choice prompts), see KEY

    def _out(self, text: str) -> None:
        if self.echo:
            sys.stdout.write(text)
            sys.stdout.flush()

    def __call__(self, timeout: float) -> Optional[str]:
        import msvcrt
        import time

        deadline = time.monotonic() + timeout
        while True:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if self.keys_mode:
                    if ch == "\x1b":
                        return KEY + "escape"
                    if ch in ("\x00", "\xe0"):
                        second = msvcrt.getwch()
                        if ch == "\x00" and second == "\x0f":
                            return KEY + "shift_tab"
                        name = _WIN_KEYS.get(second)
                        if name:
                            return KEY + name
                        continue
                    if ch == "\x03":
                        return None
                    name = _key_name(ch)
                    if name is not None:
                        return KEY + name
                    continue
                if ch in ("\r", "\n"):
                    self._out("\n")
                    line, self.buf = "".join(self.buf), []
                    return line
                if ch == "\x08":  # backspace
                    if self.buf:
                        self.buf.pop()
                        self._out("\b \b")
                    continue
                if ch == "\x1b":  # Esc: stop the turn
                    return ESCAPE
                if ch in CONTROL_KEYS:
                    return ch
                if ch == "\x03":  # Ctrl-C: the console signals the process itself
                    self.buf = []
                    return None
                if ch in ("\x00", "\xe0"):  # a function/arrow key: drop the pair
                    second = msvcrt.getwch()
                    if second == "H":  # ...except Up: edit a queued message
                        return UP
                    if ch == "\x00" and second == "\x0f":  # ...and Shift+Tab
                        return SHIFT_TAB
                    continue
                self.buf.append(ch)
                self._out(ch)
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
        import termios

        termios.tcgetattr(sys.stdin.fileno())
    except Exception:  # noqa: BLE001 - no termios: line mode, no Esc
        try:
            import select  # noqa: F401,F811
        except ImportError:
            return None
        return _posix_poll
    return _PosixKeys()


# ---------------------------------------------------------------------------
class TypeAhead:
    """Collects lines typed while a turn runs; routes one to a waiting prompt."""

    def __init__(self, poll: Optional[Callable[[float], Optional[str]]] = None,
                 on_queue: Optional[Callable[[str, int], None]] = None,
                 on_escape: Optional[Callable[[], None]] = None,
                 keys: Optional[Dict[str, Callable[[], None]]] = None,
                 intercept: Optional[Callable[[str], bool]] = None):
        self._poll = poll
        self._on_queue = on_queue
        self._on_escape = on_escape
        self._keys = dict(keys or {})
        # A typed line the front-end handles at once instead of queueing
        # (True = consumed), e.g. /btw, which is answered while the turn runs.
        self._intercept = intercept
        # Messages typed mid-turn, oldest first. A deque (not a Queue) so the
        # Up arrow can take the newest one back to edit it.
        self._queued: "deque[str]" = deque()
        self._qlock = threading.Lock()
        self._answers: "queue.Queue[str]" = queue.Queue()
        self._want_answer = threading.Event()
        self._key_q: "queue.Queue[str]" = queue.Queue()
        self._want_keys = threading.Event()
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
        close = getattr(self._poll, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 - restoring the terminal is best effort
                pass

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
            if line.startswith(KEY):
                if self._want_keys.is_set():
                    self._key_q.put(line[len(KEY):])
                continue
            if line == ESCAPE:
                self.escape()
                continue
            if line == UP and UP not in self._keys:
                self.edit_last()
                continue
            if line in CONTROL_KEYS:
                fn = self._keys.get(line)
                if fn is not None:
                    try:
                        fn()
                    except Exception:  # noqa: BLE001 - a key handler must not end type-ahead
                        pass
                continue
            self.feed(line)

    def escape(self) -> None:
        """Esc: answer a waiting prompt with "no", otherwise stop the turn."""
        if self._want_answer.is_set():
            self._want_answer.clear()
            self._answers.put("n")
            return
        if self._on_escape:
            self._on_escape()

    def feed(self, line: str) -> None:
        """Deliver one typed line (also the test entry point)."""
        if self._want_answer.is_set():
            self._want_answer.clear()
            self._answers.put(line)
            return
        text = line.strip()
        if not text:
            return
        if self._intercept is not None:
            try:
                if self._intercept(text):
                    return
            except Exception:  # noqa: BLE001 - fall back to queueing it
                pass
        with self._qlock:
            self._queued.append(text)
            waiting = len(self._queued)
        if self._on_queue:
            self._on_queue(text, waiting)

    def edit_last(self) -> Optional[str]:
        """Up arrow: take the newest queued message back into the line being
        typed (before anything already typed there), so it can be edited and
        sent again with Enter. None when nothing is queued or no line is kept."""
        buf = getattr(self._poll, "buf", None)
        if self._want_answer.is_set() or not isinstance(buf, list):
            return None
        with self._qlock:
            if not self._queued:
                return None
            text = self._queued.pop()
        typed = "".join(buf)
        buf[:] = list(text + (" " + typed if typed else ""))
        return text

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

    # -- key mode (choice prompts) ---------------------------------------------
    @property
    def supports_keys(self) -> bool:
        """The reader can hand over single keys (arrows, Enter, Esc)."""
        return self._poll is not None and hasattr(self._poll, "keys_mode")

    def begin_keys(self) -> None:
        while not self._key_q.empty():
            self._key_q.get_nowait()
        self._want_keys.set()
        self._poll.keys_mode = True  # type: ignore[union-attr]

    def end_keys(self) -> None:
        self._want_keys.clear()
        if self._poll is not None and hasattr(self._poll, "keys_mode"):
            self._poll.keys_mode = False  # type: ignore[union-attr]

    def next_key(self, timeout: Optional[float] = None) -> Optional[str]:
        """The next key in key mode; None on timeout or once type-ahead ended."""
        try:
            return self._key_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def push_key(self, key: str) -> None:
        """Deliver a key as if typed (also the test entry point)."""
        if self._want_keys.is_set():
            self._key_q.put(key)

    def requeue(self, text: str) -> None:
        """Put a line back: it was typed as a message, not as an answer."""
        if text.strip():
            with self._qlock:
                self._queued.append(text.strip())

    def drain(self) -> List[str]:
        with self._qlock:
            out = list(self._queued)
            self._queued.clear()
        return out

    @property
    def pending(self) -> int:
        return len(self._queued)

    @property
    def queued_items(self) -> List[str]:
        with self._qlock:
            return list(self._queued)

    @property
    def can_draw(self) -> bool:
        """True when the reader collects keys itself (so a box can show them)."""
        return isinstance(getattr(self._poll, "buf", None), list)

    def set_echo(self, on: bool) -> None:
        if self.can_draw:
            self._poll.echo = on  # type: ignore[union-attr]

    @property
    def typing(self) -> str:
        """The line being typed right now (not consumed)."""
        buf = getattr(self._poll, "buf", None)
        return "".join(buf) if buf else ""

    @property
    def partial(self) -> str:
        """A line the user was still typing when the turn ended. The caller
        pre-fills the next prompt with it so nothing typed is lost."""
        buf = getattr(self._poll, "buf", None)
        if not buf:
            return ""
        text, buf[:] = "".join(buf), []
        return text
