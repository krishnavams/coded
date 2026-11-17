"""Terminal resizes (a window moved between a laptop screen and a monitor,
maximized, a scaling change) must not leave the input box's old frame on
screen. Most emulators reflow on resize: a row wider than the new width is
re-wrapped, so the live areas must count the rows the terminal now shows.

`ReflowTerminal` is a small model of such an emulator (rows with a soft-wrap
flag, re-wrapped as logical lines on resize, the cursor kept on its
character), fed the real escape sequences of rich and prompt_toolkit.
"""
from __future__ import annotations

import io
import re

import pytest
from rich.text import Text

from coded import termreflow, ui
from coded.lineedit import LineEditor

# -- a reflowing terminal ------------------------------------------------------
_CSI = re.compile(r"\x1b\[([?<>=]?)([0-9;]*)([@-~])")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")


class ReflowTerminal(io.StringIO):
    """A terminal of `columns`, unbounded downwards (no scrolling to model),
    that re-wraps long rows on resize when `reflow` is set and cuts them
    otherwise (xterm, st, the Linux console)."""

    def __init__(self, columns: int, reflow: bool = True) -> None:
        super().__init__()
        self.columns, self.reflow = columns, reflow
        self.rows: list = [[]]      # each row: list of chars (None = blank)
        self.wrapped: list = [False]  # the row continues on the next one
        self.r = self.c = 0
        self.autowrap = True

    # stream interface used by rich / prompt_toolkit
    def isatty(self) -> bool:
        return False

    def write(self, data: str) -> int:
        i = 0
        while i < len(data):
            if data[i] == "\x1b":
                m = _OSC.match(data, i)
                if m:
                    i = m.end()
                    continue
                m = _CSI.match(data, i)
                if m:
                    self._csi(*m.groups())
                    i = m.end()
                    continue
                i += 2  # ESC 7 / ESC 8 / ESC = …: not used for layout here
                continue
            self._char(data[i])
            i += 1
        return len(data)

    def flush(self) -> None:
        pass

    def _row(self, r: int) -> list:
        while len(self.rows) <= r:
            self.rows.append([])
            self.wrapped.append(False)
        return self.rows[r]

    def _char(self, ch: str) -> None:
        if ch == "\n":  # the tty's ONLCR: CR + LF
            self.r, self.c = self.r + 1, 0
            self._row(self.r)
        elif ch == "\r":
            self.c = 0
        elif ch == "\b":
            self.c = max(0, min(self.c, self.columns - 1) - 1)
        elif ch >= " ":
            if self.c >= self.columns:
                if not self.autowrap:
                    self.c = self.columns - 1
                else:
                    self.wrapped[self.r] = True
                    self.r, self.c = self.r + 1, 0
            row = self._row(self.r)
            row.extend([None] * (self.c + 1 - len(row)))
            row[self.c] = ch
            self.c += 1

    def _csi(self, private: str, params: str, final: str) -> None:
        nums = [int(p) if p else 0 for p in params.split(";")] if params else []
        n = max(1, nums[0]) if nums else 1
        if private == "?":
            if params == "7":
                self.autowrap = final == "h"
            return
        col = min(self.c, self.columns - 1)
        if final == "A":
            self.r, self.c = max(0, self.r - n), col
        elif final == "B":
            self.r, self.c = self.r + n, col
            self._row(self.r)
        elif final == "C":
            self.c = min(self.columns - 1, col + n)
        elif final == "D":
            self.c = max(0, col - n)
        elif final == "G":
            self.c = max(0, n - 1)
        elif final == "K":
            row = self._row(self.r)
            mode = nums[0] if nums else 0
            if mode == 2:
                row.clear()
            elif mode == 0:
                del row[col:]
            self.wrapped[self.r] = False if mode in (0, 2) else self.wrapped[self.r]
        elif final == "J":
            del self._row(self.r)[col:]
            self.wrapped[self.r] = False
            del self.rows[self.r + 1:], self.wrapped[self.r + 1:]

    def resize(self, columns: int) -> None:
        """What the emulator does to its contents when the window resizes."""
        lines, cursor = [], None
        for r, row in enumerate(self.rows):
            if r == 0 or not self.wrapped[r - 1]:
                lines.append([])
            start = len(lines[-1])
            cells = row + [None] * ((self.columns if self.wrapped[r] else 0) - len(row))
            if r == self.r:
                cursor = (len(lines) - 1, start + min(self.c, self.columns))
            lines[-1].extend(cells)
        self.rows, self.wrapped = [], []
        for index, line in enumerate(lines):
            while line and line[-1] is None:
                line.pop()
            if not self.reflow:
                line = line[:columns]
            chunks = [line[i:i + columns] for i in range(0, len(line), columns)] or [[]]
            first = len(self.rows)
            for k, chunk in enumerate(chunks):
                self.rows.append(list(chunk))
                self.wrapped.append(k < len(chunks) - 1)
            if cursor and cursor[0] == index:
                offset = cursor[1]
                if not self.reflow:
                    self.r, self.c = first, min(offset, columns)
                else:
                    k = offset // columns
                    if k and offset % columns == 0 and offset >= len(line):
                        k, offset = k - 1, columns  # pending wrap at the row's end
                    self.r, self.c = first + k, offset - k * columns
        self.columns = columns

    def screen(self) -> list:
        """The rows as text, trailing blank rows dropped."""
        out = ["".join(ch or " " for ch in row).rstrip() for row in self.rows]
        while out and not out[-1]:
            out.pop()
        return out


def test_the_model_terminal_reflows_and_keeps_the_cursor():
    t = ReflowTerminal(10)
    t.write("0123456789\nabc")
    t.resize(4)
    assert t.screen() == ["0123", "4567", "89", "abc"]
    assert (t.r, t.c) == (3, 3)
    t.resize(10)
    assert t.screen() == ["0123456789", "abc"]
    cut = ReflowTerminal(10, reflow=False)
    cut.write("0123456789\nabc")
    cut.resize(4)
    assert cut.screen() == ["0123", "abc"]


# -- pure helpers --------------------------------------------------------------
def test_rows_for_counts_rewrapped_rows():
    assert termreflow.rows_for(0, 80) == 1
    assert termreflow.rows_for(80, 80) == 1
    assert termreflow.rows_for(81, 80) == 2
    assert termreflow.rows_for(120, 50) == 3
    assert termreflow.reflowed_rows([120, 10, 0], 80) == 4


@pytest.mark.parametrize("env, expected", [
    ({}, True),
    ({"TERM": "xterm-256color"}, True),            # most emulators say xterm
    ({"TERM": "xterm-256color", "XTERM_VERSION": "XTerm(390)"}, False),
    ({"TERM": "linux"}, False),
    ({"TERM": "st-256color"}, False),
    ({"TERM": "linux", "TMUX": "/tmp/tmux-0/default,1,0"}, True),
    ({"TERM": "linux", termreflow.ENV: "1"}, True),
    ({"TERM": "xterm-kitty", termreflow.ENV: "0"}, False),
])
def test_terminal_reflows_guess(env, expected):
    assert termreflow.terminal_reflows(env) is expected


# -- the turn dock (rich Live) ---------------------------------------------------
def _console(term: ReflowTerminal):
    return ui.make_console(file=term, width=term.columns, height=40, force_terminal=True,
                           force_interactive=True, color_system=None)


def _resize(term: ReflowTerminal, console, columns: int) -> None:
    term.resize(columns)
    console.size = (columns, 40)


def _dock_frames(screen: list) -> int:
    return sum(1 for line in screen if line.startswith("╭"))


def _run_dock(monkeypatch, reflows, widths, reflow_terminal=True):
    term = ReflowTerminal(120, reflow=reflow_terminal)
    con = _console(term)
    monkeypatch.setattr(ui, "console", con)
    for i in range(5):
        con.print(f"transcript line {i}")
    editor = LineEditor()
    editor.insert("a follow-up typed during the turn, long enough to wrap at the narrow width")
    dock = ui.TurnDock(get_input=editor.snapshot, get_mode=lambda: "default")
    live = termreflow.reflow_safe(ui.live(get_renderable=dock.render, console=con,
                                          auto_refresh=False, transient=True,
                                          redirect_stdout=False, redirect_stderr=False),
                                  reflows=reflows)
    screens = []
    live.start()
    live.refresh()
    for columns in widths:
        _resize(term, con, columns)
        live.refresh()
        con.print(f"printed at {columns}")  # agent output while the dock is up
        screens.append(term.screen())
    live.stop()
    return term, screens, editor


DISPLAYS = [80, 50, 120, 95, 64, 200]  # laptop ↔ monitor, maximize, scaling


def test_dock_leaves_no_old_frame_after_resizes(monkeypatch):
    term, screens, editor = _run_dock(monkeypatch, True, DISPLAYS)
    for columns, screen in zip(DISPLAYS, screens):
        assert _dock_frames(screen) == 1, (columns, screen)
        spinners = [line for line in screen if "esc to interrupt)" in line]
        assert len(spinners) == 1, (columns, screen)
        assert [line for line in screen if line.startswith("transcript line")] == [
            f"transcript line {i}" for i in range(5)]
        # the typed text is all there, once, inside the box
        inside = " ".join(line.strip("│ ") for line in screen if line.startswith("│"))
        assert " ".join(inside.split()).startswith("> " + editor.text)
        assert all(len(line) <= columns for line in screen)
    # transient: after the turn only the transcript and what was printed remain
    assert term.screen() == [f"transcript line {i}" for i in range(5)] + [
        f"printed at {c}" for c in DISPLAYS]


def test_without_the_fix_the_model_terminal_shows_the_bug(monkeypatch):
    """Control: rich's own count leaves a stale frame (what users saw)."""
    _, screens, _ = _run_dock(monkeypatch, False, [80])
    assert _dock_frames(screens[0]) > 1


def test_terminal_that_cuts_lines_keeps_richs_behaviour(monkeypatch):
    term, screens, _ = _run_dock(monkeypatch, False, [80, 120], reflow_terminal=False)
    assert all(_dock_frames(s) == 1 for s in screens)


def test_unchanged_width_emits_richs_own_codes():
    from rich.live_render import LiveRender

    term = ReflowTerminal(100)
    con = _console(term)
    ours = termreflow.ReflowLiveRender(Text("x" * 100 + "\nshort"), reflows=True)
    stock = LiveRender(Text("x" * 100 + "\nshort"))
    con.print(ours)
    con.print(stock)
    assert ours.position_cursor().segment == stock.position_cursor().segment
    assert ours.restore_cursor().segment == stock.restore_cursor().segment
    con.size = (150, 40)  # wider: nothing re-wraps
    assert ours.position_cursor().segment == stock.position_cursor().segment
    con.size = (50, 40)   # 100 cells now take 2 rows
    assert ours.position_cursor().segment.text.count("\x1b[1A") == 2
    assert ours.restore_cursor().segment.text.count("\x1b[1A") == 3


# -- dock contents fit the width -------------------------------------------------
def test_hints_fold_at_the_indent_and_are_unchanged_when_they_fit():
    hints = ["enter queues a message", "esc to interrupt", "ctrl+o expand", "ctrl+t tasks"]
    assert ui.fold_hints(hints, 200) == ("  enter queues a message  ·  esc to interrupt"
                                         "  ·  ctrl+o expand  ·  ctrl+t tasks")
    folded = ui.fold_hints(hints, 50).split("\n")
    assert len(folded) == 2 and all(line.startswith("  ") and len(line) <= 50
                                    for line in folded)


def test_suggestion_rows_stay_one_row_each_at_any_width(monkeypatch):
    from coded.suggest import Suggestion

    items = [Suggestion(text=f"/cmd{i} ", display=f"/cmd{i}", kind="command", start=0, end=0,
                        description="a long description that goes on " * 4)
             for i in range(4)]
    for columns in (40, 60, 200):
        term = ReflowTerminal(columns)
        con = _console(term)
        monkeypatch.setattr(ui, "console", con)
        con.print(ui.suggestion_rows(items))
        screen = term.screen()
        assert len(screen) == 4, (columns, screen)
        assert all(len(line) <= columns for line in screen)


# -- the idle prompt (prompt_toolkit) --------------------------------------------
def test_idle_prompt_box_redraws_cleanly_after_resizes():
    from prompt_toolkit import PromptSession
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.vt100 import Vt100_Output

    from coded.repl import _box_input

    term = ReflowTerminal(120)
    size = {"columns": 120}
    output = Vt100_Output(term, lambda: Size(rows=30, columns=size["columns"]), term="xterm")
    typed = "a long prompt that keeps going and going and wraps past the edge of the window, yes"
    screens = {}
    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=output)
        assert _box_input(session, lambda: "  status line")
        assert termreflow.install_prompt_reflow(session.app, reflows=True)
        app = session.app

        def resize(columns):
            term.resize(columns)
            size["columns"] = columns
            app._on_resize()

        def drive():
            app.renderer.render(app, app.layout)
            for columns in (80, 50, 120, 95):
                resize(columns)
                app.renderer.render(app, app.layout)
                screens[columns] = term.screen()
            pipe.send_text("\r")

        pipe.send_text(typed)
        pipe.send_text("\x1b[D" * 5)  # the cursor in the middle of the text
        app.pre_run_callables.append(lambda: app.loop.call_soon(
            lambda: app.loop.call_later(0.2, drive)))
        result = session.prompt("> ")
    assert result == typed
    assert sorted(screens) == [50, 80, 95, 120]
    for columns, screen in screens.items():
        assert sum(1 for line in screen if line.startswith("╭")) == 1, (columns, screen)
        assert sum(1 for line in screen if line.startswith("╰")) == 1, (columns, screen)
        assert all(len(line) <= columns for line in screen)
        inside = "".join(line[1:-1].strip() + " " for line in screen if line.startswith("│"))
        assert " ".join(inside.split()).replace(" ", "") == ("> " + typed).replace(" ", "")


def test_prompt_reflow_moves_the_remembered_cursor():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.screen import Char, Screen

    screen = Screen()
    for x, ch in enumerate("╭" + "─" * 118 + "╮"):
        screen.data_buffer[0][x] = Char(ch)
    for x, ch in enumerate("│ > hi" + " " * 113 + "│"):
        screen.data_buffer[1][x] = Char(ch)
    seen = []

    class FakeApp:
        renderer = type("R", (), {})()
        output = type("O", (), {"get_size": lambda self: type("S", (), {"columns": 80})()})()

        def _on_resize(self):
            seen.append(self.renderer._cursor_pos)

    app = FakeApp()
    app.renderer._last_screen = screen
    app.renderer._cursor_pos = Point(x=6, y=1)
    assert termreflow.install_prompt_reflow(app, reflows=True)
    app._on_resize()
    assert seen == [Point(x=6, y=2)]  # the top edge now takes two rows
    assert termreflow.install_prompt_reflow(FakeApp(), reflows=False) is False


# -- the console size follows the window ------------------------------------------
def test_console_size_is_live_on_a_terminal_despite_exported_columns(monkeypatch):
    import os

    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("LINES", "40")
    monkeypatch.setattr(ui, "_stdout_is_tty", lambda: True)
    current = {"size": os.terminal_size((120, 40))}
    monkeypatch.setattr(os, "get_terminal_size", lambda *a: current["size"])
    con = ui.make_console()
    assert (con.size.width, con.size.height) == (120, 40)
    current["size"] = os.terminal_size((80, 24))  # moved to the laptop screen
    assert (con.size.width, con.size.height) == (80, 24)
    # not a terminal: COLUMNS still decides, as before
    monkeypatch.setattr(ui, "_stdout_is_tty", lambda: False)
    assert ui.make_console().size.width == 120
