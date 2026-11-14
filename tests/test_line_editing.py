"""The mid-turn input box is a real line editor: cursor, selection, multi-line.

Before, its key readers kept an append-only list and dropped every arrow,
Home/End and Delete sequence, so text could only be typed or erased at the end.
"""

from __future__ import annotations

import io
import os
import time

import pytest

from coded import ui
from coded.lineedit import LineEditor
from coded.typeahead import UP, TypeAhead, _WindowsPoll

LEFT, RIGHT, UPK, DOWN = "\x1b[D", "\x1b[C", "\x1b[A", "\x1b[B"
HOME, END, DEL = "\x1b[H", "\x1b[F", "\x1b[3~"
S_LEFT, S_RIGHT, S_HOME, S_END = "\x1b[1;2D", "\x1b[1;2C", "\x1b[1;2H", "\x1b[1;2F"
C_LEFT, C_RIGHT, A_LEFT, A_RIGHT = "\x1b[1;5D", "\x1b[1;5C", "\x1bb", "\x1bf"
BS = "\x7f"


# -- the editor ------------------------------------------------------------------

def test_left_and_right_move_one_character_and_stop_at_the_ends():
    e = LineEditor("abc")
    assert e.cursor == 3
    e.right()
    assert e.cursor == 3                       # already at the end
    for want in (2, 1, 0, 0):
        e.left()
        assert e.cursor == want                # one step each, stops at 0
    e.right()
    assert e.cursor == 1


def test_typing_and_deleting_in_the_middle_keep_the_rest_and_the_cursor():
    e = LineEditor("hello world")
    for _ in range(6):
        e.left()
    e.insert(",")
    assert (e.text, e.cursor) == ("hello, world", 6)
    e.insert("XY")
    assert (e.text, e.cursor) == ("hello,XY world", 8)
    e.backspace()
    assert (e.text, e.cursor) == ("hello,X world", 7)
    e.delete()                                  # forward: the space goes
    assert (e.text, e.cursor) == ("hello,Xworld", 7)
    e.home()
    assert not e.backspace() and e.text == "hello,Xworld"   # nothing before the start
    e.end()
    assert not e.delete() and e.cursor == len(e.text)       # nothing after the end


def test_home_end_and_word_jumps():
    e = LineEditor("run the  api_tests now")
    e.home()
    assert e.cursor == 0
    e.word_right()
    assert e.cursor == 3                        # end of "run"
    e.word_right()
    assert e.cursor == 7                        # end of "the"
    e.word_right()
    assert e.cursor == 18                       # "api_tests" is one word
    e.word_left()
    assert e.cursor == 9
    e.end()
    assert e.cursor == len(e.text)
    e.delete_word_back()
    assert e.text == "run the  api_tests "
    e.home()
    e.delete_word_forward()
    assert e.text == " the  api_tests "


def test_shift_selection_is_replaced_or_deleted_and_arrows_collapse_it():
    e = LineEditor("hello world")
    e.left(select=True)
    e.left(select=True)
    assert e.selection() == (9, 11)
    e.insert("X")
    assert (e.text, e.cursor, e.selection()) == ("hello worX", 10, None)
    e.home(select=True)
    assert e.selection() == (0, 10)
    e.right()                                   # collapses to the selection's end
    assert (e.cursor, e.selection()) == (10, None)
    e.word_left(select=True)
    e.backspace()
    assert e.text == "hello "
    e.home()
    e.end(select=True)
    e.delete()
    assert e.text == "" and e.cursor == 0
    e.insert("abc")
    e.left(select=True)
    e.left()                                    # collapses to the selection's start
    assert (e.cursor, e.selection()) == (2, None)


def test_multi_line_up_down_keep_the_column_and_home_end_are_per_line():
    e = LineEditor("first line\nhi\nthird line")
    e.set_text(e.text, cursor=8)                # "first li|ne"
    assert e.down() and e.cursor == 13          # "hi|" (line is short)
    assert e.down() and e.cursor == 22          # back to column 8 on line 3
    assert not e.down()                         # last line: unmoved
    assert e.up() and e.up() and e.cursor == 8
    assert not e.up() and e.on_first_line
    e.down()
    e.home()
    assert e.cursor == 11                       # start of "hi"
    e.home()
    assert e.cursor == 11                       # again: stays (never jumps lines)
    e.set_text(e.text, cursor=12)
    e.end()
    assert e.cursor == 13                       # end of "hi"
    e.end()
    assert e.cursor == 13                       # again: stays
    e.text_end()
    assert e.cursor == len(e.text)              # Ctrl+End: end of the text
    e.text_start(select=True)
    assert e.selection() == (0, len(e.text))
    e.set_text(e.text, cursor=13)
    e.kill_to_line_start()
    assert e.text == "first line\n\nthird line"
    e.kill_to_line_start()                      # at a line start: joins lines
    assert e.text == "first line\nthird line"
    e.kill_to_line_end()                        # at a line end: joins lines
    assert e.text == "first linethird line"


def test_long_and_unicode_text():
    e = LineEditor("é" * 500 + "🙂" + "z" * 500)
    for _ in range(501):
        e.left()
    assert e.text[e.cursor] == "🙂"
    e.delete()
    assert "🙂" not in e.text and len(e.text) == 1000 and e.cursor == 500


# -- the POSIX terminal reader, with real key sequences ---------------------------

@pytest.fixture
def term():
    if os.name == "nt":
        pytest.skip("POSIX terminal reader")
    import pty

    from coded.typeahead import _PosixKeys

    master, slave = pty.openpty()
    reader = _PosixKeys(fd=slave, out=io.StringIO())
    reader.echo = False
    assert reader(0) is None                    # terminal modes first, as in a turn

    def type_(keys: str):
        """Type `keys`; what the reader handed over (lines, UP, …) meanwhile."""
        os.write(master, keys.encode())
        got = []
        quiet_until = time.monotonic() + 0.2
        while time.monotonic() < quiet_until:
            out = reader(0.05)
            if out is not None:
                got.append(out)
                quiet_until = time.monotonic() + 0.2
        return got

    try:
        yield reader, type_
    finally:
        reader.close()
        os.close(master)
        os.close(slave)


def test_arrow_keys_edit_in_the_middle_and_enter_sends_the_whole_line(term):
    reader, type_ = term
    assert type_("hello wrld" + LEFT * 3 + "o") == []
    assert (reader.editor.text, reader.editor.cursor) == ("hello world", 8)
    type_(LEFT + LEFT + RIGHT + BS + "W")
    assert reader.editor.text == "hello World"
    assert type_(HOME + ">" + END + "!" + "\r") == [">hello World!"]
    assert reader.editor.text == ""


@pytest.mark.parametrize("home,end", [("\x1b[H", "\x1b[F"), ("\x1bOH", "\x1bOF"),
                                      ("\x1b[1~", "\x1b[4~"), ("\x1b[7~", "\x1b[8~"),
                                      ("\x01", "\x05")])
def test_every_home_end_spelling(term, home, end):
    reader, type_ = term
    type_("middle" + home + "<" + end + ">")
    assert reader.editor.text == "<middle>"


def test_delete_word_jumps_and_readline_keys(term):
    reader, type_ = term
    type_("one two three" + C_LEFT + C_LEFT + DEL)
    assert reader.editor.text == "one wo three"
    type_(A_RIGHT + A_RIGHT + A_LEFT + "\x17")           # Ctrl+W: "wo " goes
    assert reader.editor.text == "one three"
    type_("\x01" + "\x0b")                               # Ctrl+A, Ctrl+K
    assert reader.editor.text == ""
    type_("keep this" + LEFT * 4 + "\x15")               # Ctrl+U
    assert reader.editor.text == "this"
    type_("\x1b\x7f")                                    # Alt+Backspace at the start
    type_(END + "\x1b\x7f")
    assert reader.editor.text == ""


def test_shift_arrows_select_and_typing_replaces(term):
    reader, type_ = term
    type_("replace me" + S_LEFT * 2)
    assert reader.editor.selection() == (8, 10)
    type_("us")
    assert reader.editor.text == "replace us"
    type_(S_HOME + DEL)
    assert reader.editor.text == ""
    type_("abc" + "\x1b[1;6D" + BS)                      # Ctrl+Shift+←: select a word
    assert reader.editor.text == ""
    type_("xyz" + HOME + S_END + RIGHT)
    assert reader.editor.selection() is None and reader.editor.cursor == 3


def test_backslash_enter_and_alt_enter_start_a_new_line(term):
    reader, type_ = term
    assert type_("line one\\\r") == []
    type_("line two" + "\x1b\r" + "three")
    assert reader.editor.text == "line one\nline two\nthree"
    type_(UPK + UPK + END + "!")                         # ↑ moves between lines here
    assert reader.editor.text == "line one!\nline two\nthree"
    type_(END + "?")                                     # End at a line end: stays
    assert reader.editor.text == "line one!?\nline two\nthree"
    type_("\x1b[1;5F" + "." + "\x1b[1;5H" + "^")         # Ctrl+End, Ctrl+Home
    assert reader.editor.text == "^line one!?\nline two\nthree."
    assert type_(UPK) == [UP]                            # on the first line: edit queued
    type_(DOWN + DOWN + END)
    assert type_("\r") == ["^line one!?\nline two\nthree."]


def test_an_arrow_split_across_reads_still_counts():
    """ESC can reach the reader before the rest of its sequence; the reader
    waits up to 30 ms for it (a lone ESC is the interrupt key)."""
    if os.name == "nt":
        pytest.skip("POSIX terminal reader")
    import pty
    import threading

    from coded.typeahead import _PosixKeys

    master, slave = pty.openpty()
    reader = _PosixKeys(fd=slave, out=io.StringIO())
    reader.echo = False
    try:
        reader(0)
        os.write(master, b"ab\x1b")
        threading.Timer(0.005, lambda: os.write(master, b"[DX")).start()
        results = [reader(0.2) for _ in range(5)]
        assert results == [None] * 5 and reader.editor.text == "aXb"
    finally:
        reader.close()
        os.close(master)
        os.close(slave)


def test_unknown_sequences_are_skipped_whole(term):
    reader, type_ = term
    type_("ok" + "\x1b[15~" + "\x1b[24;5~" + "\x1bOP" + "!")   # F5, Ctrl+F12, F1
    assert reader.editor.text == "ok!"


def test_echo_mode_redraws_the_line_on_the_terminal(term):
    reader, type_ = term
    reader.echo = True
    reader.out = io.StringIO()
    type_("abd" + LEFT + "c" + HOME + "x" + DEL + END + BS + "D")
    assert reader.editor.text == "xbcD"
    assert _screen(reader.out.getvalue()) == ["xbcD"]


def _screen(output: str) -> list:
    """A minimal terminal: printable characters overwrite, \\b moves left."""
    rows, row, col = [[]], 0, 0
    for ch in output:
        if ch == "\b":
            col = max(0, col - 1)
        elif ch == "\n":
            rows.append([])
            row, col = row + 1, 0
        else:
            line = rows[row]
            line.extend(" " * (col - len(line) + 1))
            line[col] = ch
            col += 1
    return ["".join(r).rstrip() for r in rows]


# -- the Windows console reader (keys injected) ------------------------------------

def _windows(keys, shift=False):
    reader = _WindowsPoll(shift_held=lambda: shift)
    reader.echo = False
    queue = list(keys)
    reader._kbhit = lambda: bool(queue)
    reader._getwch = lambda: queue.pop(0)
    return reader


W_LEFT, W_RIGHT, W_HOME, W_END, W_DEL = "\xe0K", "\xe0M", "\xe0G", "\xe0O", "\xe0S"


def test_windows_arrows_home_end_delete_and_word_jumps():
    r = _windows("hello wrld" + W_LEFT * 3 + "o" + W_HOME + ">" + W_END + "!")
    assert r(0.05) is None and r.editor.text == ">hello world!"
    r = _windows("one two" + "\xe0s" + W_DEL + "\xe0t" + "\x08" + "\r")
    assert r(0.05) == "one w"                            # Ctrl+←, Del, Ctrl+→, ⌫
    r = _windows("x" + "\x00K" + "y")                    # numpad arrows use "\x00"
    assert r(0.05) is None and r.editor.text == "yx"
    r = _windows("a\nb" + "\xe0w" + "<" + "\xe0u" + ">")  # Ctrl+Home / Ctrl+End
    assert r(0.05) is None and r.editor.text == "<a\nb>"


def test_windows_shift_selection_and_ctrl_enter_new_line():
    r = _windows("pick" + W_LEFT * 2, shift=True)
    r(0.05)
    assert r.editor.selection() == (2, 4)
    r = _windows("one" + "\n" + "two" + "\xe0H" + "\r")  # Ctrl+Enter, ↑, Enter
    assert r(0.05) == "one\ntwo"
    r = _windows("solo" + "\xe0H")
    assert r(0.05) == UP                                  # ↑ on the first line


# -- TypeAhead and the input box ----------------------------------------------------

def test_completion_and_edit_last_put_the_cursor_at_the_end():
    r = _windows("")
    ta = TypeAhead(poll=r, complete=lambda t: "/review " if t == "/rev" else None)
    r.editor.set_text("/rev")
    r.editor.home()
    assert ta.complete() == "/review " and ta.editing.cursor == len("/review ")
    ta.feed("queued one")
    r.editor.set_text("typed")
    r.editor.home()
    assert ta.edit_last() == "queued one"
    assert (ta.editing.text, ta.editing.cursor) == ("queued one typed", 16)
    assert ta.partial == "queued one typed" and ta.typing == ""


def _cursor_index(text):
    """Where the block cursor is drawn in an editor_text line (plain index)."""
    for span in text.spans:
        if "reverse" in str(span.style):
            return span.start
    return None


def test_the_input_box_draws_the_cursor_where_it_is():
    line = ui.editor_text("abc", 1)
    assert line.plain == "> abc" and _cursor_index(line) == 3        # on "b"
    assert _cursor_index(ui.editor_text("abc")) == 5                # after the end
    multi = ui.editor_text("one\ntwo", 3)
    assert multi.plain == "> one \n  two" and _cursor_index(multi) == 5
    sel = ui.editor_text("hello", 5, (1, 3))
    assert [(s.start, s.end) for s in sel.spans if "on " in str(s.style)] == [(3, 5)]
    big = ui.editor_text("x" * 5000, 2500)
    assert len(big.spans) == 2                  # the "> " and the cursor: not one per char


def test_the_dock_draws_a_snapshot_with_its_cursor(monkeypatch):
    con = ui.TranscriptConsole(width=80, height=30, record=True, color_system=None,
                               file=io.StringIO())
    monkeypatch.setattr(ui, "console", con)
    editor = LineEditor("first\nsecond")
    editor.left()
    dock = ui.TurnDock(editor.snapshot)
    con.print(dock.render())
    text = con.export_text()
    assert "> first" in text and "  second" in text


# -- the prompt between turns is prompt_toolkit's, and stays untouched ---------------

def test_the_normal_prompt_still_edits_in_the_middle(tmp_path, fake_server, headless):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from coded.config import Config
    from coded.repl import Repl
    from tests.test_agent_e2e import _make_agent

    with fake_server(lambda b: ({"role": "assistant", "content": "ok"}, None)) as server:
        agent = _make_agent(tmp_path, server.base_url)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                repl = Repl(agent, Config())
                pipe.send_text("helo wrld" + LEFT * 3 + "o" + HOME + RIGHT * 3 + "l"
                               + END + S_LEFT + "D" + "\r")
                assert repl.prompt.prompt() == "hello worlD"
                pipe.send_text("first\\\r" + "second" + UPK + END + "!" + "\r")
                assert repl.prompt.prompt() == "first!\nsecond"
