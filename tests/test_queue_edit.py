"""Editing a queued message: ↑ takes the newest one back into the input."""

from __future__ import annotations

import io
import os
import time

import pytest

from coded import ui
from coded.lineedit import LineEditor
from coded.typeahead import DOWN, UP, TypeAhead


class _Keys:
    def __init__(self, lines=()):
        self.editor, self.echo = LineEditor(), True
        self._lines = list(lines)

    def __call__(self, _t):
        return self._lines.pop(0) if self._lines else None


def test_up_takes_the_newest_queued_message_back_to_edit():
    keys = _Keys()
    ta = TypeAhead(poll=keys)
    ta.feed("run the api tests")
    ta.feed("then the ui tests")
    keys.editor.set_text("and")
    assert ta.edit_last() == "then the ui tests"
    assert ta.typing == "then the ui tests and"        # put before what was typed
    assert ta.queued_items == ["run the api tests"]
    assert ta.edit_last() == "run the api tests"     # ↑ again: the one before
    assert ta.typing == "run the api tests then the ui tests and"
    assert ta.edit_last() is None and ta.pending == 0
    ta.feed("run only the api tests")                 # Enter queues the edit
    assert ta.drain() == ["run only the api tests"]


def test_up_does_nothing_while_a_prompt_waits_for_an_answer():
    keys = _Keys()
    ta = TypeAhead(poll=keys)
    ta.feed("queued")
    ta._want_answer.set()
    assert ta.edit_last() is None and ta.queued_items == ["queued"]


def test_the_reader_thread_handles_up_itself():
    keys = _Keys(["first", "second", UP])
    ta = TypeAhead(poll=keys)
    with ta:
        time.sleep(0.3)
    assert ta.queued_items == ["first"] and ta.typing == "second"


def test_the_up_arrow_is_recognised_from_the_terminal():
    if os.name == "nt":
        pytest.skip("POSIX terminal reader")
    import pty

    from coded.typeahead import _PosixKeys

    master, slave = pty.openpty()
    reader = _PosixKeys(fd=slave, out=io.StringIO())
    try:
        # Terminal modes first, as in a real turn (the reader runs before any
        # key is typed): with IEXTEN still on, macOS's tty eats ^O on input.
        assert reader(0) is None
        os.write(master, b"x\x1b[A")
        assert reader(0.5) == UP
        os.write(master, b"\x1bOA\x1b[B")       # application-mode Up; Down: next history
        assert reader(0.5) == UP
        assert reader(0.3) == DOWN and reader.editor.text == "x"
    finally:
        reader.close()
        os.close(master)
        os.close(slave)


def _plain(renderable) -> str:
    from rich.console import Console

    con = Console(width=100, record=True, color_system=None,
                  file=io.StringIO())
    con.print(renderable)
    return con.export_text()


def test_the_dock_lists_queued_messages_with_the_edit_hint():
    items = [f"message {i}" for i in range(1, 6)]
    dock = ui.TurnDock(lambda: "", get_queued=lambda: len(items), get_queue=lambda: items)
    text = _plain(dock.render())
    assert "⏎ queued (5)  ↑ to edit" in text and "… 2 older" in text
    assert "3. message 3" in text and "5. message 5" in text and "message 2" not in text
    assert "↑ edit queued" in text


def test_messages_left_from_earlier_turns_stay_editable(headless, tmp_path, monkeypatch):
    from coded.repl import Repl
    from tests.test_progress_and_typeahead import _repl_agent, _repl_config

    agent = _repl_agent(tmp_path)
    repl = Repl(agent, _repl_config(tmp_path))
    ran, seen_queue = [], []

    def fake_run(text, images=None):
        ran.append(text)
        seen_queue.append(repl._typeahead.queued_items)
        if text == "one":
            repl._typeahead.feed("two")
            repl._typeahead.feed("three")
        elif text == "two":                       # "three" waits here: edit it
            repl._typeahead._poll.editor.set_text("")
            assert repl._typeahead.edit_last() == "three"
            repl._typeahead.feed("three, but only the api tests")
        return "ok"

    monkeypatch.setattr(agent, "run", fake_run)
    monkeypatch.setattr("coded.repl.default_poll", lambda: _Keys())
    monkeypatch.setattr(repl.prompt, "prompt", lambda *a, **k: (_ for _ in ()).throw(EOFError))
    repl.run(initial="one")
    assert ran == ["one", "two", "three, but only the api tests"]
    assert seen_queue[1] == ["three"]


@pytest.mark.asyncio
async def test_tui_up_edits_the_newest_queued_message(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from coded.tui import CodedTUI
    from tests.test_progress_and_typeahead import _repl_agent

    app = CodedTUI(_repl_agent(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app._queued[:] = ["first", "second"]
        prompt = app.query_one("#prompt", Input)
        prompt.focus()
        await pilot.press("up")
        await pilot.pause()
        assert prompt.value == "second" and app._queued == ["first"]
        await pilot.press("up")
        await pilot.pause()
        assert prompt.value == "first second" and app._queued == []
        await pilot.press("up")                      # nothing left: no change
        assert prompt.value == "first second"
