"""Redraw correctly after the terminal is resized (a window moved to another
display, maximized, a font or scaling change).

Both live areas of the REPL redraw in place by moving the cursor up over what
they drew last: the turn dock (a rich `Live`) by the rows of its last frame,
the idle prompt (prompt_toolkit) by the cursor row it remembers. Most terminal
emulators (iTerm2, Terminal.app, VTE/GNOME, Konsole, kitty, Alacritty,
WezTerm, Windows Terminal, VS Code, tmux) *reflow* on resize: a row wider than
the new width is re-wrapped onto several rows. The input box's edges span the
full width, so after a narrower resize every one of them takes two rows or
more, the cursor-up count falls short, and the top of the old frame stays on
screen: a stray edge or spinner line above the box, broken lines, the box
seeming to grow. This module counts rows the way the terminal now lays them
out, so the old frame is erased exactly.

Terminals that cut lines instead of re-wrapping them (xterm, st, the Linux
console) keep one row per line; there the stock behaviour is already right.
`CODED_TERMINAL_REFLOW=1|0` overrides the guess.
"""
from __future__ import annotations

import os
from typing import Iterable, List, Mapping, Optional

from rich.control import Control, ControlType
from rich.live_render import LiveRender
from rich.segment import Segment

ENV = "CODED_TERMINAL_REFLOW"

# TERM prefixes of terminals known not to reflow on resize.
_NO_REFLOW_TERMS = ("linux", "st", "vt", "cons")


def terminal_reflows(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether this terminal re-wraps long rows when it gets narrower."""
    env = os.environ if env is None else env
    forced = (env.get(ENV) or "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    if env.get("TMUX"):
        return True  # tmux reflows whatever terminal it runs in
    if env.get("XTERM_VERSION"):
        return False  # the real xterm (other emulators only borrow TERM=xterm*)
    term = (env.get("TERM") or "").lower()
    return not any(term == t or term.startswith(t + "-") for t in _NO_REFLOW_TERMS)


def rows_for(width: int, columns: int) -> int:
    """Rows a line `width` cells wide takes in a terminal `columns` wide."""
    if columns <= 0 or width <= columns:
        return 1
    return -(-width // columns)


def reflowed_rows(widths: Iterable[int], columns: int) -> int:
    return sum(rows_for(w, columns) for w in widths)


# -- rich: the turn dock and the other Live displays -------------------------
class ReflowLiveRender(LiveRender):
    """A `LiveRender` that remembers the width of each line it drew, so the
    next redraw (and the final erase) climbs over as many rows as the
    terminal now shows them on. While the width is unchanged (or grew) the
    control codes are exactly rich's own."""

    def __init__(self, *args, reflows: Optional[bool] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.reflows = terminal_reflows() if reflows is None else reflows
        self._widths: List[int] = []
        self._console = None

    def __rich_console__(self, console, options):
        self._console = console
        segments = list(super().__rich_console__(console, options))
        self._widths = [Segment.get_line_length(line)
                        for line in Segment.split_lines(segments)]
        yield from segments

    def _rows(self) -> Optional[int]:
        """Rows the last frame occupies now, or None to keep rich's count."""
        if not self.reflows or self._shape is None or self._console is None:
            return None
        # A legacy Windows console reserves its last column: rich reports one less.
        columns = self._console.size.width + bool(getattr(self._console, "legacy_windows", False))
        rows = reflowed_rows(self._widths, columns)
        return rows if rows != self._shape[1] else None

    def position_cursor(self) -> Control:
        rows = self._rows()
        if rows is None:
            return super().position_cursor()
        # The cursor is at the end of the last line, i.e. on its last row.
        return Control(ControlType.CARRIAGE_RETURN, (ControlType.ERASE_IN_LINE, 2),
                       *(((ControlType.CURSOR_UP, 1), (ControlType.ERASE_IN_LINE, 2))
                         * (rows - 1)))

    def restore_cursor(self) -> Control:
        rows = self._rows()
        if rows is None:
            return super().restore_cursor()
        # Live.stop() has moved to the start of the row under the frame.
        return Control(ControlType.CARRIAGE_RETURN,
                       *((ControlType.CURSOR_UP, 1), (ControlType.ERASE_IN_LINE, 2)) * rows)


def reflow_safe(live, reflows: Optional[bool] = None):
    """Give a rich `Live` (not yet started) the reflow-aware renderer."""
    try:
        old = live._live_render
        live._live_render = ReflowLiveRender(old.renderable, old.style,
                                             vertical_overflow=old.vertical_overflow,
                                             reflows=reflows)
    except Exception:  # noqa: BLE001 - another rich layout: keep its own renderer
        pass
    return live


# -- prompt_toolkit: the idle prompt ------------------------------------------
def _row_width(row: Mapping[int, object], has_style=None) -> int:
    """Cells the renderer wrote on a row: up to the last non-blank (or styled
    blank) cell, as prompt_toolkit's own screen diff decides."""
    width = 0
    for index, cell in row.items():
        char = getattr(cell, "char", " ")
        if char != " " or (has_style is not None and has_style[cell.style]):
            width = max(width, index + max(1, getattr(cell, "width", 1)))
    return width


def reflowed_cursor(rows: List[Mapping[int, object]], x: int, y: int, columns: int,
                    has_style=None) -> tuple:
    """Where the cursor at (x, y) of a drawn screen is after the terminal
    re-wrapped it at `columns`: (column, rows below the screen's first row)."""
    if columns <= 0:
        return x, y
    above = sum(rows_for(_row_width(rows[i], has_style) if i < len(rows) else 0, columns)
                for i in range(y))
    return x % columns, above + x // columns


def install_prompt_reflow(app, reflows: Optional[bool] = None) -> bool:
    """Make a prompt_toolkit Application erase its old output correctly when
    the terminal is resized. Its own resize handler moves up by the cursor row
    it remembers; that row is corrected for reflow first. Returns False (and
    changes nothing) where the terminal does not reflow or the internals
    differ."""
    if not (terminal_reflows() if reflows is None else reflows):
        return False
    original = getattr(app, "_on_resize", None)
    renderer = getattr(app, "renderer", None)
    if original is None or not hasattr(renderer, "_cursor_pos"):
        return False

    def on_resize() -> None:
        try:
            screen, pos = renderer._last_screen, renderer._cursor_pos
            if screen is not None:
                columns = app.output.get_size().columns
                rows = [screen.data_buffer[i] for i in range(pos.y + 1)]
                x, y = reflowed_cursor(rows, pos.x, pos.y, columns,
                                       getattr(renderer, "_style_string_has_style", None))
                renderer._cursor_pos = type(pos)(x=x, y=y)
        except Exception:  # noqa: BLE001 - fall back to prompt_toolkit's own erase
            pass
        original()

    app._on_resize = on_resize
    return True
