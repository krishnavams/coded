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
    assert ui.todo_bar(ITEMS, 8) == "▰▰▨▱▱▱▱▱"  # ▨ is the item being worked on
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

def _tui_agent(tmp_path, base_url):
    """A headless agent for the TUI (local, so a bare `pytest` run needs no
    tests package on sys.path)."""
    from coded.agent import create_agent
    from coded.config import Config, ModelConfig
    from coded.permissions import PermissionManager

    cfg = Config()
    model = ModelConfig(name="fake", model="fake", provider="openai-compatible",
                        base_url=base_url, api_key="k")
    cfg.add_model(model, make_default=True)
    return create_agent(model=model, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(auto_approve=True),
                        stream=False, verbose=False)


@pytest.mark.asyncio
async def test_the_tui_queues_a_question_asked_mid_turn(tmp_path, fake_server):
    pytest.importorskip("textual")
    from coded.tui import CodedTUI

    with fake_server(lambda body: ({"role": "assistant", "content": "done"},
                                   {"prompt_tokens": 1, "completion_tokens": 1,
                                    "total_tokens": 2})) as srv:
        app = CodedTUI(_tui_agent(tmp_path, srv.base_url))
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


def test_a_follow_up_typed_at_the_permission_prompt_is_not_an_answer(headless, tmp_path,
                                                                     monkeypatch):
    """Pressing Enter on a follow-up just as the prompt appears must not
    approve the action — and must not lose the message either."""
    from coded.permissions import PermissionManager
    from coded.repl import Repl
    from coded.typeahead import TypeAhead

    repl = Repl(_repl_agent(tmp_path), _repl_config(tmp_path))
    ta = TypeAhead(poll=lambda _t: None)
    repl._typeahead = ta
    ta.feed("also delete the old tests")  # queued, not an answer

    import threading

    answers = []
    t = threading.Thread(target=lambda: answers.append(
        repl._permission_prompt("Run shell command", "rm -rf build")))
    t.start()
    while not ta._want_answer.is_set():
        pass
    ta.feed("always do that")   # not an exact answer: re-queued, asked again
    while not ta._want_answer.is_set():
        pass
    ta.feed("n")
    t.join(timeout=2)

    assert answers == ["n"]
    assert ta.drain() == ["also delete the old tests", "always do that"]


def _repl_config(tmp_path):
    from coded.config import Config, ModelConfig

    cfg = Config()
    cfg.add_model(ModelConfig(name="m", model="m", base_url="http://x/v1", api_key="k"),
                  make_default=True)
    return cfg


def _repl_agent(tmp_path, cfg=None):
    from coded.agent import create_agent
    from coded.permissions import PermissionManager

    cfg = cfg or _repl_config(tmp_path)
    return create_agent(model=cfg.get_model(), config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(), stream=False, verbose=False)


def test_setup_folds_the_new_config_into_the_running_session(headless, tmp_path, monkeypatch):
    """/setup must not replace the live Config (that would drop --yolo and the
    other CLI overrides) nor pin the model (that would disable new routing)."""
    import json as _json

    from coded.repl import Repl

    cfg = _repl_config(tmp_path)
    cfg.auto_approve = True            # as if --yolo had been passed
    cfg.max_turns = 7
    agent = _repl_agent(tmp_path, cfg)
    repl = Repl(agent, cfg)

    config_file = tmp_path / "config.json"
    config_file.write_text(_json.dumps({
        "models": {"m": {"model": "m", "base_url": "http://x/v1", "api_key": "k"},
                   "big": {"model": "big", "base_url": "http://x/v1", "api_key": "k"}},
        "default_model": "m",
        "routing": {"enabled": True, "hard": "big"},
    }))
    monkeypatch.setenv("CODED_CONFIG", str(config_file))
    monkeypatch.setattr("coded.config_wizard.run_wizard", lambda **kw: 0)

    repl._cmd_setup()

    assert repl.config is cfg                  # same object, updated in place
    assert cfg.auto_approve is True and cfg.max_turns == 7  # overrides survived
    assert set(cfg.models) == {"m", "big"}
    assert agent.routing_on is True            # not pinned off by the reselect


@pytest.mark.asyncio
async def test_a_queued_message_actually_runs_and_a_queued_command_acts_as_one(
        tmp_path, fake_server):
    pytest.importorskip("textual")
    from coded.tui import CodedTUI

    seen = []

    def script(body):
        import time

        text = body["messages"][-1]["content"]
        seen.append(text)
        if text == "first":
            time.sleep(0.4)  # long enough to type while it runs
        return ({"role": "assistant", "content": "ok"},
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    with fake_server(script) as srv:
        app = CodedTUI(_tui_agent(tmp_path, srv.base_url))
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt")
            prompt.value = "first"
            await pilot.press("enter")
            await pilot.pause()
            assert app.busy  # the turn is running, as a slow one would be
            for typed in ("/plan", "second"):
                prompt.value = typed
                await pilot.press("enter")
            assert app._queued == ["/plan", "second"]
            for _ in range(200):
                await pilot.pause()
                if not app.busy and not app._queued:
                    break
    assert seen[0] == "first"
    assert seen[-1].startswith("second")  # the queued message really ran
    assert app.agent.mode == "plan"      # and the queued command acted as a command


# -- against a real terminal --------------------------------------------------

@pytest.mark.skipif(not hasattr(__import__("os"), "openpty"), reason="needs a pty")
def test_type_ahead_reads_a_real_terminal():
    """Every other test feeds TypeAhead directly; this one drives the real
    reader through a pty, which is what it faces in a terminal."""
    import os
    import sys
    import time

    from coded.typeahead import TypeAhead, default_poll

    primary, secondary = os.openpty()
    saved = sys.stdin
    try:
        sys.stdin = os.fdopen(secondary, "r")
        poll = default_poll()
        assert poll is not None  # a pty is a terminal
        with TypeAhead(poll) as ta:
            os.write(primary, b"a question typed mid-turn\n")
            deadline = time.monotonic() + 3
            while ta.pending == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ta.drain() == ["a question typed mid-turn"]

            # and a prompt takes the next line instead
            import threading

            answers = []
            t = threading.Thread(target=lambda: answers.append(ta.ask(timeout=3)))
            t.start()
            while not ta._want_answer.is_set():
                time.sleep(0.01)
            os.write(primary, b"y\n")
            t.join(timeout=3)
            assert answers == ["y"]

        # The reader is gone, so the next line is still there for the next prompt.
        os.write(primary, b"typed at the next prompt\n")
        time.sleep(0.2)
        assert sys.stdin.readline().rstrip() == "typed at the next prompt"
    finally:
        sys.stdin = saved
        os.close(primary)
