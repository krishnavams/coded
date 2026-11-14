"""Two things a long turn needs: visible task progress, and the ability to type
while the agent is busy.
"""

from __future__ import annotations

import json
import time

import pytest

from coded import ui
from coded.lineedit import LineEditor
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
            time.sleep(1.5)  # long enough to type while it runs, even on a busy machine
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


# -- the input box during a turn --------------------------------------------

def _plain(renderable, width: int = 80) -> str:
    from rich.console import Console

    con = Console(width=width, record=True, color_system=None, force_terminal=False)
    con.print(renderable)
    return con.export_text()


class _Keys:
    """A key-by-key reader stand-in: has `editor` and `echo` like the real ones."""

    def __init__(self):
        self.editor, self.echo = LineEditor(), True

    def __call__(self, _t):
        return None


def test_the_turn_dock_shows_an_input_box_with_what_is_being_typed():
    keys = _Keys()
    ta = TypeAhead(poll=keys)
    keys.editor.set_text("also check tests")
    dock = ui.TurnDock(lambda: ta.typing, lambda: ITEMS, lambda: 2)
    text = _plain(dock.render())
    assert "> also check tests" in text
    assert "Fix the bug" in text            # the unfinished tasks stay docked
    assert "↑ edit queued" in text and "esc to interrupt" in text
    assert ta.typing == "also check tests"  # peeking does not consume the line
    assert ta.partial == "also check tests"


def test_a_waiting_prompt_changes_the_box_hint():
    dock = ui.TurnDock(lambda: "y")
    dock.asking(True)
    text = _plain(dock.render())
    assert "Waiting for your answer" in text and "enter to answer" in text


def test_thinking_relabels_the_dock_instead_of_opening_a_second_live():
    dock = ui.TurnDock(lambda: "").start()
    try:
        assert ui.active_dock() is dock
        with ui.thinking("Compacting"):
            assert "Compacting…" in _plain(dock.render())
        text = _plain(dock.render())
        assert any(f"{w}…" in text for w in ui.SPINNER_WORDS) and "esc to interrupt" in text
    finally:
        dock.stop()
    assert ui.active_dock() is None


def test_echo_is_switched_off_only_for_readers_that_collect_keys():
    keys = _Keys()
    ta = TypeAhead(poll=keys)
    assert ta.can_draw
    ta.set_echo(False)
    assert keys.echo is False
    line_mode = TypeAhead(poll=lambda _t: None)
    assert not line_mode.can_draw
    line_mode.set_echo(False)  # no-op, no error


_CMDS = {"/help": ("Show this help.", "built-in"),
         "/model": ("Show or switch the active model.", "built-in"),
         "/models": ("List all configured models.", "built-in"),
         "/review": ("Run the review [x] pipeline.", "built-in")}


def _menu(tmp_path):
    from coded.suggest import Context, Engine, MenuState, UsageStore

    engine = Engine(Context(cwd=str(tmp_path), commands=dict(_CMDS)),
                    UsageStore(str(tmp_path), tmp_path / "usage.json"))
    return MenuState(engine)


def _dock_for(menu, typed):
    return ui.TurnDock(lambda: typed, get_suggestions=lambda: (
        menu.items(typed, len(typed)), menu.index))


def test_typing_a_slash_mid_turn_lists_matching_commands(tmp_path):
    menu = _menu(tmp_path)
    text = _plain(_dock_for(menu, "/mo").render())
    assert "/model " in text and "/models" in text and "/help" not in text
    assert "tab/enter take" in text and "esc close" in text
    # every command for a bare "/", the description shown as plain text
    text = _plain(_dock_for(menu, "/").render())
    assert "/help" in text and "/review" in text and "[x]" in text
    # plain text has none; a full command alone has none; asking hides them
    for typed in ("fix the bug", "/review"):
        text = _plain(_dock_for(menu, typed).render())
        assert "Show this help" not in text and "tab/enter take" not in text
    dock = _dock_for(menu, "/mo")
    dock.asking(True)
    assert "/models" not in _plain(dock.render())


def test_tab_takes_the_best_suggestion_mid_turn(tmp_path):
    keys = _Keys()
    keys.echo = False
    ta = TypeAhead(poll=keys, menu=_menu(tmp_path))
    keys.editor.set_text("/rvw")                     # fuzzy: letters in order
    assert ta.complete() == "/review " and ta.typing == "/review "
    assert ta.pending == 0  # completing does not queue anything
    keys.editor.set_text("hello")
    assert ta.complete() is None and ta.typing == "hello"


def test_the_posix_reader_routes_keys_to_an_open_dropdown(tmp_path):
    import io
    import os

    if os.name == "nt":
        pytest.skip("POSIX terminal reader")
    import pty

    from coded.typeahead import _PosixKeys

    master, slave = pty.openpty()
    reader = _PosixKeys(fd=slave, out=io.StringIO())
    reader.echo = False
    escaped = []
    ta = TypeAhead(poll=reader, menu=_menu(tmp_path), on_escape=lambda: escaped.append(1))

    def wait_for(cond):
        for _ in range(100):
            if cond():
                return True
            time.sleep(0.02)
        return False

    try:
        with ta:
            os.write(master, b"/rev\t")                       # Tab takes the first row
            assert wait_for(lambda: ta.typing == "/review ")
            reader.editor.set_text("")
            os.write(master, b"/mo\x1b[B\x1b[B\r")           # ↓ ↓ picks the 2nd, Enter takes it
            assert wait_for(lambda: ta.typing == "/models ")
            assert ta.pending == 0                            # ...and did not send it
            os.write(master, b"\r")                           # Enter again sends it
            assert wait_for(lambda: ta.pending == 1)
            os.write(master, b"/he\x1b")                      # Esc closes the dropdown...
            assert wait_for(lambda: ta.typing == "/he")
            time.sleep(0.1)
            assert escaped == []                              # ...without stopping the turn
            os.write(master, b"\x1b")                         # closed now: Esc stops the turn
            assert wait_for(lambda: escaped == [1])
        assert ta.drain() == ["/models"]
    finally:
        reader.close()
        os.close(master)
        os.close(slave)


def test_the_dock_spinner_rotates_words_and_shows_elapsed_time():
    words = {ui.spinner_word(t) for t in range(0, 400, 4)}
    assert len(words) == len(ui.SPINNER_WORDS)          # every word comes round
    assert ui.spinner_word(0) == ui.spinner_word(3.9)   # steady for a few seconds
    assert ui.spinner_word(0) != ui.spinner_word(4)
    dock = ui.TurnDock(lambda: "")
    dock._started -= 75
    text = _plain(dock.render())
    assert "1m 15s · esc to interrupt" in text
    dock.set_activity("Compacting")                     # a specific label wins
    assert "Compacting…" in _plain(dock.render())
