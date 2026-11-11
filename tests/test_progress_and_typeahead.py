"""Two things a long turn needs: visible task progress, and the ability to type
while the agent is busy.
"""

from __future__ import annotations

import json

import pytest

from coded import ui
from coded.typeahead import TypeAhead

ITEMS = [
    {"content": "Read the code", "status": "completed"},
    {"content": "Fix the bug", "status": "in_progress"},
    {"content": "Write tests", "status": "pending"},
    {"content": "Update docs", "status": "pending"},
]


# -- progress ---------------------------------------------------------------

def test_progress_counts_only_completed_items():
    assert ui.todo_progress(ITEMS) == (1, 4, 25)
    assert ui.todo_progress([]) == (0, 0, 0)
    assert ui.todo_progress([{"status": "completed"}] * 3) == (3, 3, 100)


def test_the_bar_fills_with_progress_and_is_empty_without_tasks():
    assert ui.todo_bar(ITEMS, 8) == "▰▰▱▱▱▱▱▱"
    assert ui.todo_bar([{"status": "completed"}] * 2, 4) == "▰▰▰▰"
    assert ui.todo_bar([]) == ""


def test_the_title_carries_the_whole_summary():
    title = ui.todo_title(ITEMS)
    assert title.startswith("Tasks  1/4") and title.endswith("25%")
    assert ui.todo_title([]) == "Tasks"


def test_the_current_task_is_the_in_progress_one():
    assert ui.current_todo(ITEMS) == "Fix the bug"
    assert ui.current_todo([{"content": "x", "status": "pending"}]) == ""


def test_the_panel_shows_the_count_and_marks_the_running_item(capsys):
    ui.todos(ITEMS)
    out = capsys.readouterr().out
    assert "1/4" in out and "25%" in out
    assert "← now" in out
    assert "Fix the bug" in out


# -- typing while the agent works -------------------------------------------

def test_lines_typed_during_a_turn_are_queued_in_order():
    noted = []
    ta = TypeAhead(poll=lambda _t: None, on_queue=lambda text, n: noted.append((text, n)))
    ta.feed("and then deploy it")
    ta.feed("   ")  # blank input is not a message
    ta.feed("what about windows?")
    assert ta.pending == 2
    assert noted == [("and then deploy it", 1), ("what about windows?", 2)]
    assert ta.drain() == ["and then deploy it", "what about windows?"]
    assert ta.drain() == []


def test_a_waiting_prompt_takes_the_next_line_instead_of_the_queue():
    """The permission prompt and the type-ahead reader must not race for the
    same keystrokes."""
    import threading

    ta = TypeAhead(poll=lambda _t: None)
    ta.feed("typed before the question")  # meant for the agent, stays queued
    answers = []
    asker = threading.Thread(target=lambda: answers.append(ta.ask(timeout=2)))
    asker.start()
    while not ta._want_answer.is_set():  # the question is now on screen
        pass
    ta.feed("y")
    asker.join(timeout=2)
    assert answers == ["y"]
    assert ta.drain() == ["typed before the question"]


def test_the_reader_thread_stops_with_the_turn():
    polls = []

    def poll(timeout):
        polls.append(timeout)
        return None

    with TypeAhead(poll=poll) as ta:
        assert ta.available
        for _ in range(100000):
            if polls:
                break
    assert polls  # it really ran
    assert ta._thread is None  # and it is gone once the turn ends


def test_type_ahead_is_off_without_a_terminal(monkeypatch):
    from coded import typeahead

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert typeahead.default_poll() is None
    ta = TypeAhead(typeahead.default_poll())
    assert ta.available is False
    assert ta.drain() == []
    ta.start()  # a no-op, not a crash
    ta.stop()


def test_posix_poll_reads_a_line(tmp_path, monkeypatch):
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("select() does not work on Windows stdin")
    path = tmp_path / "in.txt"
    path.write_text("hello there\n")
    with path.open() as fh:
        monkeypatch.setattr("sys.stdin", fh)
        from coded.typeahead import _posix_poll

        assert _posix_poll(1.0) == "hello there"
        assert _posix_poll(1.0) is None  # EOF, not an empty line forever


# -- the TUI queues mid-turn input too ---------------------------------------

@pytest.mark.asyncio
async def test_the_tui_queues_a_question_asked_mid_turn(tmp_path, fake_server):
    pytest.importorskip("textual")
    from coded.tui import CodedTUI
    from tests.test_tui import _agent  # the shared headless agent factory

    with fake_server(lambda body: ({"role": "assistant", "content": "done"},
                                   {"prompt_tokens": 1, "completion_tokens": 1,
                                    "total_tokens": 2})) as srv:
        app = CodedTUI(_agent(tmp_path, srv.base_url))
        async with app.run_test() as pilot:
            app.busy = True  # pretend a turn is running
            prompt = app.query_one("#prompt")
            prompt.value = "what about windows?"
            await pilot.press("enter")
            await pilot.pause()
            assert app._queued == ["what about windows?"]
            assert prompt.value == ""  # and the input is not left stuck


# -- the REPL runs what was typed mid-turn, in order --------------------------

def test_the_repl_runs_messages_typed_during_a_turn(headless, tmp_path, monkeypatch):
    """Typing a follow-up while the agent works used to be swallowed by the
    busy terminal; it is queued and run as the next turn."""
    from coded.agent import create_agent
    from coded.config import Config, ModelConfig
    from coded.permissions import PermissionManager
    from coded.repl import Repl

    cfg = Config()
    m = ModelConfig(name="m", model="m", base_url="http://x/v1", api_key="k")
    cfg.add_model(m, make_default=True)
    agent = create_agent(model=m, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(), stream=False, verbose=False)
    repl = Repl(agent, cfg)

    ran = []

    def fake_run(text, images=None, route_hint=None):
        ran.append(text)
        if text == "first":  # the user types two follow-ups while this runs
            repl._typeahead.feed("second")
            repl._typeahead.feed("third")
        return "ok"

    monkeypatch.setattr(agent, "run", fake_run)
    monkeypatch.setattr("coded.repl.default_poll", lambda: lambda _t: None)
    # After the queue drains, the prompt ends the REPL.
    monkeypatch.setattr(repl.prompt, "prompt", lambda *a, **k: (_ for _ in ()).throw(EOFError))

    repl.run(initial="first")
    assert ran == ["first", "second", "third"]


def test_the_repl_drops_the_queue_when_a_turn_is_interrupted(headless, tmp_path, monkeypatch):
    from coded.agent import create_agent
    from coded.config import Config, ModelConfig
    from coded.permissions import PermissionManager
    from coded.repl import Repl

    cfg = Config()
    m = ModelConfig(name="m", model="m", base_url="http://x/v1", api_key="k")
    cfg.add_model(m, make_default=True)
    agent = create_agent(model=m, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(), stream=False, verbose=False)
    repl = Repl(agent, cfg)

    ran = []

    def fake_run(text, images=None, route_hint=None):
        ran.append(text)
        repl._typeahead.feed("never mind")
        raise KeyboardInterrupt

    monkeypatch.setattr(agent, "run", fake_run)
    monkeypatch.setattr("coded.repl.default_poll", lambda: lambda _t: None)
    monkeypatch.setattr(repl.prompt, "prompt", lambda *a, **k: (_ for _ in ()).throw(EOFError))

    repl.run(initial="first")
    assert ran == ["first"]  # Ctrl-C means "stop", including what was queued
