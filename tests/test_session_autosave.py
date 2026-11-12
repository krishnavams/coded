"""A session survives a crash mid-turn: it is saved after every step, written
atomically, and a step cut short is closed when the session is loaded."""

from __future__ import annotations

import json

from coded.session import Session
from coded.sessions_store import INTERRUPTED_RESULT, SessionStore, close_tool_calls
from tests.test_agent_e2e import _make_agent, _script_write_then_done


def test_each_step_of_a_turn_is_saved(tmp_path, fake_server, monkeypatch):
    monkeypatch.setenv("CODED_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    saved = []
    script = _script_write_then_done("f.txt", "x")
    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)

        def autosave():
            store.save(agent.session, session_id="s1", cwd=str(tmp_path), model="fake")
            saved.append([m["role"] for m in agent.session.messages])

        agent.on_step = autosave
        agent.run("create f.txt")
    # The user message, the tool step and the final answer each hit disk.
    assert saved[0][-1] == "user"
    assert saved[1][-2:] == ["assistant", "tool"]
    assert saved[2][-1] == "assistant"
    assert store.latest(str(tmp_path)) == "s1"
    assert len(store.load("s1").messages) == len(agent.session.messages)


def test_a_crash_before_the_turn_ends_keeps_what_was_done(tmp_path, fake_server, monkeypatch):
    monkeypatch.setenv("CODED_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    script = _script_write_then_done("f.txt", "x")
    calls = []

    class Killed(BaseException):
        pass

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)

        def autosave():
            store.save(agent.session, session_id="s1", cwd=str(tmp_path), model="fake")
            calls.append(1)
            if len(calls) == 2:      # the process dies right after the tool step
                raise Killed

        agent.on_step = autosave
        try:
            agent.run("create f.txt")
        except Killed:
            pass
    loaded = store.load("s1")
    roles = [m["role"] for m in loaded.messages]
    assert roles == ["system", "user", "assistant", "tool"]


def test_saving_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("CODED_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    s = Session(system_prompt="SYS")
    s.add_user("hi")
    store.save(s, session_id="a", cwd="/p", model="m")
    files = sorted(p.name for p in (tmp_path / "sessions").iterdir())
    assert files == ["a.json"]  # no temp file left behind
    json.loads((tmp_path / "sessions" / "a.json").read_text())


def test_unfinished_tool_calls_are_closed():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    fixed = close_tool_calls(msgs)
    assert [m.get("tool_call_id") for m in fixed[3:]] == ["a", "b"]
    assert fixed[4]["content"] == INTERRUPTED_RESULT
    assert close_tool_calls(fixed) == fixed       # a valid history is unchanged
    assert fixed[0] is msgs[0]                   # same message objects (turn marks)


def test_the_next_turn_after_an_interrupted_tool_is_valid(tmp_path, fake_server):
    seen = []

    def script(body):
        seen.append(body["messages"])
        return {"role": "assistant", "content": "ok"}, {"prompt_tokens": 1,
                                                        "completion_tokens": 1}

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        agent.session.add_user("earlier")
        agent.session.messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "z", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]})
        agent.run("again")
    roles = [m["role"] for m in seen[-1]]
    assert roles[-3:] == ["assistant", "tool", "user"]
