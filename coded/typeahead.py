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
from typing import Any, Callable, Dict, List, Optional, Tuple

from coded.lineedit import MOVES as MOVE_KEYS, LineEditor, Snapshot, apply as apply_edit

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
# Tab: complete the slash command being typed.
TAB = "\t"
CONTROL_KEYS = (CTRL_B, CTRL_O, CTRL_T, UP, SHIFT_TAB, TAB)
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



# Escape sequences of the mid-turn line editor (after ESC), as
# (editing key, Shift held). CSI modifiers: 2 Shift, 3 Alt, 4 Alt+Shift,
# 5 Ctrl, 6 Ctrl+Shift — Ctrl/Alt with an arrow jumps a word.
_EDIT_SEQS = {"[D": ("left", False), "OD": ("left", False),
              "[C": ("right", False), "OC": ("right", False),
              "[A": ("up", False), "OA": ("up", False),
              "[B": ("down", False), "OB": ("down", False),
              "[H": ("home", False), "OH": ("home", False), "[1~": ("home", False),
              "[7~": ("home", False), "[F": ("end", False), "OF": ("end", False),
              "[4~": ("end", False), "[8~": ("end", False), "[3~": ("delete", False),
              "[3;5~": ("delete_word_forward", False), "[3;3~": ("delete_word_forward", False),
              # Alt (meta) + key, as macOS Terminal/iTerm send Option+←/→/⌫.
              "b": ("word_left", False), "f": ("word_right", False),
              "B": ("word_left", True), "F": ("word_right", True),
              "d": ("delete_word_forward", False), "\x7f": ("delete_word_back", False),
              "\x08": ("delete_word_back", False), "\r": ("newline", False),
              "\n": ("newline", False)}
_ARROW_WORD = {"D": "word_left", "C": "word_right"}
_ARROW = {"D": "left", "C": "right", "A": "up", "B": "down", "H": "home", "F": "end"}
_TEXT_ENDS = {"H": "text_start", "F": "text_end"}  # Ctrl+Home / Ctrl+End


def _edit_sequence(seq: str) -> Optional[Tuple[str, bool]]:
    """The editing key an escape sequence (without its ESC) stands for."""
    if seq in _EDIT_SEQS:
        return _EDIT_SEQS[seq]
    # Modified keys: ESC [ 1 ; <mod> <letter>  (xterm and most terminals).
    if seq.startswith("[1;") and len(seq) >= 5 and seq[-1] in _ARROW:
        try:
            mod = int(seq[3:-1])
        except ValueError:
            return None
        shift, alt, ctrl = bool((mod - 1) & 1), bool((mod - 1) & 2), bool((mod - 1) & 4)
        letter = seq[-1]
        if (ctrl or alt) and letter in _ARROW_WORD:
            return _ARROW_WORD[letter], shift
        if ctrl and letter in _TEXT_ENDS:
            return _TEXT_ENDS[letter], shift
        return _ARROW[letter], shift
    return None


# Control characters of the line editor (readline's): Ctrl+A/E start/end of
# line, Ctrl+K/U kill to end/start, Ctrl+W delete the word before, Ctrl+D
# delete forward, Ctrl+F forward a character. (Ctrl+B is taken: it moves a
# running sub-agent to the background.)
_EDIT_CTRL = {"\x01": "home", "\x05": "end", "\x0b": "kill_to_line_end",
              "\x15": "kill_to_line_start", "\x17": "delete_word_back",
              "\x04": "delete", "\x06": "right"}


class _LineEditing:
    """The editing half of a key reader: a `LineEditor`, echo when no input box
    is drawn, and Enter (a `\\` before the cursor makes Enter a new line)."""

    editor: LineEditor
    echo: bool
    # A suggestion dropdown (`suggest.MenuState`), set by TypeAhead. While it
    # is open, ↑/↓ move in it, Enter takes a picked row and Esc closes it.
    menu: Any = None

    def _write(self, text: str) -> None:  # pragma: no cover - per reader
        raise NotImplementedError

    def _menu_open(self) -> bool:
        if self.menu is None:
            return False
        snap = self.editor.snapshot()
        try:
            return self.menu.is_open(snap.text, snap.cursor)
        except Exception:  # noqa: BLE001 - a broken menu must not block typing
            return False

    def _escape(self) -> Optional[str]:
        """A lone Esc: close an open dropdown, else hand ESCAPE on (stop the turn)."""
        if self._menu_open():
            self.menu.dismiss(self.editor.text)
            return None
        return ESCAPE

    def _edit(self, key: str, select: bool = False) -> Optional[str]:
        """Apply one editing key; the reader's return value (UP on ↑ in the
        first line, which edits a queued message), else None."""
        before = self.editor.snapshot()
        if key in ("up", "down") and not select and self._menu_open():
            self.menu.move(before.text, before.cursor, -1 if key == "up" else 1)
            return None
        if key == "up" and not select and self.editor.on_first_line:
            return UP
        if key == "down" and not self.editor.multiline:
            return None
        apply_edit(self.editor, key, select)
        self._echo_edit(before)
        return None

    def _type(self, ch: str) -> None:
        before = self.editor.snapshot()
        self.editor.insert(ch)
        self._echo_edit(before)

    def _enter(self) -> Optional[str]:
        """Enter: hand the text over, or — after a backslash — start a new line."""
        snap = self.editor.snapshot()
        if self.menu is not None and self._menu_open():
            if self.menu.accept(self.editor, explicit=True) is not None:
                self._echo_edit(snap)
                return None  # a picked suggestion goes into the line, never runs
        if snap.selection is None and snap.text[:snap.cursor].endswith("\\"):
            self.editor.backspace()
            self.editor.newline()
            self._echo_edit(snap)
            return None
        if self.echo:
            self._write("\n")
        return self.editor.take()

    def _echo_edit(self, before: Snapshot) -> None:
        """Redraw the line being typed when it is echoed raw (no input box):
        back to where it starts, the new text, blanks over leftovers, then
        back to the cursor. Only the current line: a new line moves down."""
        if not self.echo:
            return
        after = self.editor.snapshot()
        b_start = before.text.rfind("\n", 0, before.cursor) + 1
        a_start = after.text.rfind("\n", 0, after.cursor) + 1
        out = "\b" * (before.cursor - b_start)
        if a_start != b_start or after.text[:a_start] != before.text[:b_start]:
            out += "\n" if after.text.count("\n") > before.text.count("\n") else ""
        b_line = before.text[b_start:].split("\n", 1)[0]
        a_line = after.text[a_start:].split("\n", 1)[0]
        pad = max(0, len(b_line) - len(a_line))
        out += a_line + " " * pad + "\b" * (len(a_line) + pad - (after.cursor - a_start))
        self._write(out)

# ---------------------------------------------------------------------------
# Platform readers: return a complete line, or None if nothing was typed.
# ---------------------------------------------------------------------------
class _PosixKeys(_LineEditing):
    """Key-by-key reading on a POSIX terminal, so a bare Esc can stop a turn.

    The terminal is put in non-canonical, no-echo mode for the turn (signals
    stay on, so Ctrl-C still works). Keys edit `editor` (a `LineEditor`: ←/→,
    Home/End, Delete, word jumps, Shift-selection, Ctrl+A/E/K/U/W, a new line
    with `\\`+Enter or Alt+Enter); Enter hands the text over. A lone Esc is
    returned as `ESCAPE`; ↑ on the first line as `UP`. A line still being typed
    when the turn ends stays in `editor` for the next prompt. `close()`
    restores the terminal (TypeAhead.stop calls it).
    """

    def __init__(self, fd: Optional[int] = None, out=None) -> None:
        import codecs

        self.fd = sys.stdin.fileno() if fd is None else fd
        self.out = out or sys.stdout
        self.editor = LineEditor()
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

    def _write(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except Exception:  # noqa: BLE001 - echo is cosmetic
            pass

    def _echo(self, text: str) -> None:
        if self.echo:
            self._write(text)

    def _read(self, timeout: float) -> str:
        import select

        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return ""
        data = os.read(self.fd, 1024)
        if not data:
            raise EOFError
        return self._decoder.decode(data)

    @staticmethod
    def _sequence_length(rest: str) -> int:
        """Length of the escape sequence at the start of `rest` (after ESC):
        CSI/SS3 end at a byte in @..~; anything else is ESC + one key (Alt)."""
        if rest[:1] in ("[", "O") and len(rest) > 1:
            j = 1
            while j < len(rest) and not ("@" <= rest[j] <= "~"):
                j += 1
            return j + 1
        return 1

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
                    result = self._escape()
                    if result is None:
                        continue
                    return result
                j = self._sequence_length(rest)
                seq, chunk, i = rest[:j], rest[j:], 0
                if seq == "[Z":
                    self._pending = chunk
                    return SHIFT_TAB
                edit = _edit_sequence(seq)
                if edit is not None:
                    result = self._edit(*edit)
                    if result is not None:
                        self._pending = chunk
                        return result
                continue  # an unknown sequence (F-keys, …) is skipped whole
            if ch in ("\r", "\n"):
                line = self._enter()
                if line is not None:
                    self._pending = chunk[i:]
                    return line
                continue
            if ch in ("\x7f", "\x08"):
                self._edit("backspace")
                continue
            if ch in _EDIT_CTRL:
                self._edit(_EDIT_CTRL[ch])
                continue
            if ch in CONTROL_KEYS:
                self._pending = chunk[i:]
                return ch
            if ch < " ":
                continue
            self._type(ch)
        return None


def _posix_poll(timeout: float) -> Optional[str]:
    import select

    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    line = sys.stdin.readline()
    return None if line == "" else line.rstrip("\r\n")


# Windows console arrow/function keys: the code after a "\x00"/"\xe0" prefix.
_WIN_EDIT = {"K": "left", "M": "right", "H": "up", "P": "down", "G": "home", "O": "end",
             "S": "delete", "s": "word_left", "t": "word_right", "\x93": "delete_word_forward",
             "w": "text_start", "u": "text_end"}


def _shift_held() -> bool:
    """Is Shift down right now? (The console reader gets an arrow's code but
    not its modifiers; checked as the key is read, it says whether to select.)"""
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)  # VK_SHIFT
    except Exception:  # noqa: BLE001 - no user32 (e.g. a service): never select
        return False


class _WindowsPoll(_LineEditing):
    """Console input on Windows, where stdin cannot be select()ed.

    Keys edit `editor` like the POSIX reader (arrows, Home/End, Delete,
    Ctrl+←/→, Shift-selection, Ctrl+A/E/K/U/W; Ctrl+Enter or `\\`+Enter for a
    new line) and Enter hands the text over. A line still being typed when the
    turn ends stays in `editor` and is handed to the next prompt by
    `TypeAhead.partial`.
    """

    def __init__(self, shift_held: Callable[[], bool] = _shift_held) -> None:
        self.editor = LineEditor()
        self.echo = True  # off while ui.TurnDock draws the line in its input box
        self.keys_mode = False  # one key per call (choice prompts), see KEY
        self._shift_held = shift_held

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _out(self, text: str) -> None:
        if self.echo:
            self._write(text)

    def _getwch(self) -> str:
        import msvcrt

        return msvcrt.getwch()

    def _kbhit(self) -> bool:
        import msvcrt

        return msvcrt.kbhit()

    def __call__(self, timeout: float) -> Optional[str]:
        import time

        deadline = time.monotonic() + timeout
        while True:
            while self._kbhit():
                ch = self._getwch()
                if self.keys_mode:
                    if ch == "\x1b":
                        return KEY + "escape"
                    if ch in ("\x00", "\xe0"):
                        second = self._getwch()
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
                if ch == "\r":
                    line = self._enter()
                    if line is not None:
                        return line
                    continue
                if ch == "\n":  # Ctrl+Enter: a new line
                    self._edit("newline")
                    continue
                if ch == "\x08":  # backspace
                    self._edit("backspace")
                    continue
                if ch == "\x7f":  # Ctrl+Backspace
                    self._edit("delete_word_back")
                    continue
                if ch == "\x1b":  # Esc: close the dropdown, else stop the turn
                    result = self._escape()
                    if result is None:
                        continue
                    return result
                if ch in _EDIT_CTRL:
                    self._edit(_EDIT_CTRL[ch])
                    continue
                if ch in CONTROL_KEYS:
                    return ch
                if ch == "\x03":  # Ctrl-C: the console signals the process itself
                    self.editor.set_text("")
                    return None
                if ch in ("\x00", "\xe0"):  # an arrow/function key: its code follows
                    second = self._getwch()
                    if ch == "\x00" and second == "\x0f":
                        return SHIFT_TAB
                    key = _WIN_EDIT.get(second)
                    if key is not None:
                        select = key in MOVE_KEYS and self._shift_held()
                        result = self._edit(key, select)
                        if result is not None:
                            return result
                    continue
                if ch < " ":
                    continue
                self._type(ch)
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
                 intercept: Optional[Callable[[str], bool]] = None,
                 complete: Optional[Callable[[str], Optional[str]]] = None,
                 menu: Any = None):
        self._poll = poll
        # The suggestion dropdown (suggest.MenuState): the reader routes ↑/↓,
        # Enter and Esc to it while it is open; Tab takes its row.
        self.menu = menu
        if menu is not None and poll is not None and hasattr(poll, "editor"):
            poll.menu = menu
        # Tab: the line being typed -> its completion (None: leave it).
        self._complete = complete
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
            if line == TAB:
                self.complete()
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

    def _editor(self) -> Optional[LineEditor]:
        editor = getattr(self._poll, "editor", None)
        return editor if isinstance(editor, LineEditor) else None

    def complete(self) -> Optional[str]:
        """Tab: take the dropdown's row (picked, else the first) or, without a
        dropdown, replace the line with the completer's completion."""
        editor = self._editor()
        if editor is None:
            return None
        before = editor.snapshot()
        echo = getattr(self._poll, "_echo_edit", None)
        if self.menu is not None:
            chosen = self.menu.accept(editor)
            if chosen is not None:
                if echo is not None:
                    echo(before)
                return editor.text
        if self._complete is None:
            return None
        try:
            new = self._complete(before.text)
        except Exception:  # noqa: BLE001 - a completer must not end type-ahead
            return None
        if not new or new == before.text:
            return None
        editor.set_text(new)
        if echo is not None:
            echo(before)
        return new

    def edit_last(self) -> Optional[str]:
        """Up arrow: take the newest queued message back into the line being
        typed (before anything already typed there), so it can be edited and
        sent again with Enter. None when nothing is queued or no line is kept."""
        editor = self._editor()
        if self._want_answer.is_set() or editor is None:
            return None
        with self._qlock:
            if not self._queued:
                return None
            text = self._queued.pop()
        typed = editor.text
        editor.set_text(text + (" " + typed if typed else ""))
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
        return self._editor() is not None

    def set_echo(self, on: bool) -> None:
        if self.can_draw:
            self._poll.echo = on  # type: ignore[union-attr]

    @property
    def typing(self) -> str:
        """The line being typed right now (not consumed)."""
        editor = self._editor()
        return editor.text if editor is not None else ""

    @property
    def editing(self) -> Snapshot:
        """The line being typed with its cursor and selection, for the input box."""
        editor = self._editor()
        return editor.snapshot() if editor is not None else Snapshot("", 0, None)

    @property
    def partial(self) -> str:
        """A line the user was still typing when the turn ended. The caller
        pre-fills the next prompt with it so nothing typed is lost."""
        editor = self._editor()
        return editor.take() if editor is not None else ""
