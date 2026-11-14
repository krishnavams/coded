"""/resume: list this folder's saved sessions and switch to one mid-run."""

from __future__ import annotations

import io

import pytest

from coded import ui
from coded.config import Config
from coded.repl import Repl
from coded.session import Session
from coded.sessions_store import SessionKeeper, SessionStore, resume_command
from tests.test_agent_e2e import _make_agent


def _saved(store, sid, cwd, text, updated=None, todos=None):
    s = Session(system_prompt="SYS")
    s.add_user(text)
    s.messages.append({"role": "assistant", "content": f"answer to {text}"})
    s.turns = 1
    s.todos = todos or []
    store.save(s, session_id=sid, cwd=cwd, model="fake")
    if updated is not None:  # order sessions without sleeping
        path = store.dir / f"{sid}.json"
        import json

        data = json.loads(path.read_text())
        data["updated"] = updated
        path.write_text(json.dumps(data))


@pytest.fixture
def setup(tmp_path, fake_server, monkeypatch, headless):
    con = ui.TranscriptConsole(width=200, record=True, file=io.StringIO(), color_system=None)
    monkeypatch.setattr(ui, "console", con)
    store = SessionStore()
    cwd = str(tmp_path)
    _saved(store, "old", cwd, "first [/x] thing", updated=100,
           todos=[{"content": "do it", "status": "pending", "activeForm": "doing it"}])
    _saved(store, "new", cwd, "second thing", updated=200)
    _saved(store, "elsewhere", "/some/other/dir", "not here", updated=300)
    with fake_server(lambda body: ({"role": "assistant", "content": "ok"}, None)) as server:
        agent = _make_agent(tmp_path, server.base_url)
        keeper = SessionKeeper(cwd, "fake", store, session_id="current")
        yield agent, keeper, store, con


def test_lists_this_folders_sessions_newest_first(setup):
    agent, keeper, _store, con = setup
    repl = Repl(agent, Config(), sessions=keeper)
    repl._handle_slash("/resume")
    text = con.export_text()
    assert text.index("second thing") < text.index("first [/x] thing")
    assert "not here" not in text
    assert "/resume N reopens one" in text


def test_resume_by_number_switches_and_keeps_saving_under_its_id(setup):
    agent, keeper, store, con = setup
    agent.run("hello from the current one")
    repl = Repl(agent, Config(), sessions=keeper)
    repl._handle_slash("/resume 2")  # "old": second in the list
    assert keeper.id == "old"
    assert agent.session.messages[1]["content"] == "first [/x] thing"
    assert [t["content"] for t in agent.todos.items] == ["do it"]
    assert "Resumed session old" in con.export_text()
    # The conversation left behind was saved first.
    left = store.load("current")
    assert any(m.get("content") == "hello from the current one" for m in left.messages)
    # Autosave now writes to the resumed session.
    agent.run("carry on")
    keeper.save(agent)
    assert any(m.get("content") == "carry on" for m in store.load("old").messages)
    assert not any(m.get("content") == "carry on" for m in store.load("current").messages)


def test_resume_by_id_prefix_and_other_folders(setup):
    agent, keeper, _store, _con = setup
    note, table, ok = resume_command(agent, keeper, "else")
    assert ok and keeper.id == "elsewhere"
    assert "/some/other/dir" in note
    # An empty session is not written to disk when you leave it.
    assert "previous conversation" not in note
    assert keeper.store.load("current") is None


def test_unknown_current_and_off(setup):
    agent, keeper, _store, _con = setup
    before = agent.session
    for arg in ("nope", "99"):
        note, _t, ok = resume_command(agent, keeper, arg)
        assert not ok and "No saved session" in note
    keeper.id = "new"
    note, _t, ok = resume_command(agent, keeper, "new")
    assert not ok and "you are in" in note
    assert agent.session is before
    note, _t, ok = resume_command(agent, None, "")
    assert not ok and "off" in note


def test_resume_is_listed_as_a_slash_command():
    from coded.repl import SLASH_COMMANDS

    assert "/resume" in SLASH_COMMANDS
