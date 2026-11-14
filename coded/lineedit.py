"""The text being typed in the mid-turn input box: text, cursor and selection.

`typeahead`'s key readers turn key presses into calls on a `LineEditor`, and
`ui.input_box` draws its `snapshot()`. Kept free of terminal code so every
editing rule is testable on its own.

Conventions follow readline and ordinary text fields: ←/→ move one
character, Home/End (and Ctrl+A/E) go to the start/end of the current line
(Ctrl+Home/End: of the whole text),
Ctrl/Alt+←/→ jump words, Shift extends a selection that typing, Backspace or
Delete replace, Ctrl+U/K kill to the line start/end, Ctrl+W or Alt+Backspace
delete the word before the cursor. ↑/↓ move between lines of a multi-line
message; on the first line ↑ is left to the caller (it edits a queued
message). Positions are Python string indexes (code points).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, Tuple

# Moving left/right, the cursor lands only on these boundaries for word jumps.
_WORD_CHARS = "_"


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch in _WORD_CHARS


@dataclass(frozen=True)
class Snapshot:
    text: str
    cursor: int
    selection: Optional[Tuple[int, int]]  # (start, end), start < end


class LineEditor:
    """A small multi-line text field. Thread-safe: the key reader edits it while
    the dock draws it from another thread."""

    def __init__(self, text: str = "") -> None:
        self._lock = threading.RLock()
        self._text = text
        self._cursor = len(text)
        self._anchor: Optional[int] = None  # selection start, when selecting
        self._goal_col: Optional[int] = None  # column kept while moving ↑/↓

    # -- reading ---------------------------------------------------------------
    @property
    def text(self) -> str:
        return self._text

    @property
    def cursor(self) -> int:
        return self._cursor

    def selection(self) -> Optional[Tuple[int, int]]:
        with self._lock:
            return self._selection()

    def _selection(self) -> Optional[Tuple[int, int]]:
        if self._anchor is None or self._anchor == self._cursor:
            return None
        return (min(self._anchor, self._cursor), max(self._anchor, self._cursor))

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(self._text, self._cursor, self._selection())

    def __bool__(self) -> bool:
        return bool(self._text)

    def __len__(self) -> int:
        return len(self._text)

    def __str__(self) -> str:
        return self._text

    # -- whole-text changes ------------------------------------------------------
    def set_text(self, text: str, cursor: Optional[int] = None) -> None:
        """Replace everything (completion, a recalled message); cursor at the end."""
        with self._lock:
            self._text = text
            self._cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))
            self._anchor = None
            self._goal_col = None

    def take(self) -> str:
        """The text, leaving the field empty (Enter, end of turn)."""
        with self._lock:
            text = self._text
            self.set_text("")
            return text

    # -- typing ------------------------------------------------------------------
    def insert(self, s: str) -> None:
        """Type `s` at the cursor, replacing the selection if there is one."""
        with self._lock:
            self._delete_selection()
            c = self._cursor
            self._text = self._text[:c] + s + self._text[c:]
            self._cursor = c + len(s)
            self._goal_col = None

    def backspace(self) -> bool:
        with self._lock:
            if self._delete_selection():
                return True
            if self._cursor == 0:
                return False
            return self._cut(self._cursor - 1, self._cursor)

    def delete(self) -> bool:
        """Forward delete (Delete key, Ctrl+D)."""
        with self._lock:
            if self._delete_selection():
                return True
            if self._cursor >= len(self._text):
                return False
            return self._cut(self._cursor, self._cursor + 1)

    def delete_word_back(self) -> bool:
        with self._lock:
            if self._delete_selection():
                return True
            return self._cut(self._word_left(self._cursor), self._cursor)

    def delete_word_forward(self) -> bool:
        with self._lock:
            if self._delete_selection():
                return True
            return self._cut(self._cursor, self._word_right(self._cursor))

    def kill_to_line_start(self) -> bool:
        """Ctrl+U: delete from the start of the current line to the cursor."""
        with self._lock:
            if self._delete_selection():
                return True
            start = self._line_start(self._cursor)
            if start == self._cursor and start > 0:
                start -= 1  # at a line start: join with the line above
            return self._cut(start, self._cursor)

    def kill_to_line_end(self) -> bool:
        """Ctrl+K: delete from the cursor to the end of the current line."""
        with self._lock:
            if self._delete_selection():
                return True
            end = self._line_end(self._cursor)
            if end == self._cursor and end < len(self._text):
                end += 1  # at a line end: join with the line below
            return self._cut(self._cursor, end)

    def newline(self) -> None:
        self.insert("\n")

    # -- moving --------------------------------------------------------------------
    def left(self, select: bool = False) -> None:
        with self._lock:
            sel = self._selection()
            if sel and not select:
                self._move(sel[0], False)
            else:
                self._move(self._cursor - 1, select)

    def right(self, select: bool = False) -> None:
        with self._lock:
            sel = self._selection()
            if sel and not select:
                self._move(sel[1], False)
            else:
                self._move(self._cursor + 1, select)

    def word_left(self, select: bool = False) -> None:
        with self._lock:
            self._move(self._word_left(self._cursor), select)

    def word_right(self, select: bool = False) -> None:
        with self._lock:
            self._move(self._word_right(self._cursor), select)

    def home(self, select: bool = False) -> None:
        """Start of the current line."""
        with self._lock:
            self._move(self._line_start(self._cursor), select)

    def end(self, select: bool = False) -> None:
        """End of the current line."""
        with self._lock:
            self._move(self._line_end(self._cursor), select)

    def text_start(self, select: bool = False) -> None:
        """Start of the whole text (Ctrl+Home)."""
        with self._lock:
            self._move(0, select)

    def text_end(self, select: bool = False) -> None:
        """End of the whole text (Ctrl+End)."""
        with self._lock:
            self._move(len(self._text), select)

    def up(self, select: bool = False) -> bool:
        """One line up, keeping the column; False on the first line (unmoved)."""
        with self._lock:
            start = self._line_start(self._cursor)
            if start == 0:
                return False
            col = self._column()
            prev_start = self._line_start(start - 1)
            target = min(prev_start + col, start - 1)
            self._move(target, select, keep_goal=col)
            return True

    def down(self, select: bool = False) -> bool:
        """One line down, keeping the column; False on the last line (unmoved)."""
        with self._lock:
            stop = self._line_end(self._cursor)
            if stop >= len(self._text):
                return False
            col = self._column()
            next_start = stop + 1
            target = min(next_start + col, self._line_end(next_start))
            self._move(target, select, keep_goal=col)
            return True

    @property
    def multiline(self) -> bool:
        return "\n" in self._text

    @property
    def on_first_line(self) -> bool:
        return self._line_start(self._cursor) == 0

    # -- internals -----------------------------------------------------------------
    def _move(self, pos: int, select: bool, keep_goal: Optional[int] = None) -> None:
        pos = max(0, min(pos, len(self._text)))
        if select:
            if self._anchor is None:
                self._anchor = self._cursor
        else:
            self._anchor = None
        self._cursor = pos
        self._goal_col = keep_goal

    def _column(self) -> int:
        if self._goal_col is not None:
            return self._goal_col
        return self._cursor - self._line_start(self._cursor)

    def _cut(self, start: int, end: int) -> bool:
        if start >= end:
            return False
        self._text = self._text[:start] + self._text[end:]
        self._cursor = start
        self._anchor = None
        self._goal_col = None
        return True

    def _delete_selection(self) -> bool:
        sel = self._selection()
        self._anchor = None
        if sel is None:
            return False
        return self._cut(*sel)

    def _line_start(self, pos: int) -> int:
        return self._text.rfind("\n", 0, pos) + 1

    def _line_end(self, pos: int) -> int:
        stop = self._text.find("\n", pos)
        return len(self._text) if stop < 0 else stop

    def _word_left(self, pos: int) -> int:
        t = self._text
        while pos > 0 and not _is_word(t[pos - 1]):
            pos -= 1
        while pos > 0 and _is_word(t[pos - 1]):
            pos -= 1
        return pos

    def _word_right(self, pos: int) -> int:
        t, n = self._text, len(self._text)
        while pos < n and not _is_word(t[pos]):
            pos += 1
        while pos < n and _is_word(t[pos]):
            pos += 1
        return pos


# Editing keys a reader passes to `apply`: the name, then whether Shift is held.
MOVES = {"left", "right", "home", "end", "text_start", "text_end", "word_left",
         "word_right", "up", "down"}
EDITS = {"backspace", "delete", "delete_word_back", "delete_word_forward",
         "kill_to_line_start", "kill_to_line_end", "newline"}


def apply(editor: LineEditor, key: str, select: bool = False) -> bool:
    """Run one named editing key on `editor`. False for ↑ on the first line and
    ↓ on the last, so the caller can give those keys another meaning."""
    if key in MOVES:
        result = getattr(editor, key)(select)
        return result is not False
    if key in EDITS:
        getattr(editor, key)()
        return True
    raise ValueError(f"unknown editing key {key!r}")
