"""The session event log: tool outcomes and timings, denials, turns, saved with
the session and never sent to the model."""

from __future__ import annotations

import json
import threading

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.events import EventLog, recent_problems, tool_stats
from coded.permissions import PermissionManager
from coded.session import Session
from coded.sessions_store import SessionStore

_U = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def _call(name, args, cid):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_tools_turns_and_denials_are_recorded(tmp_path, fake_server):
    (tmp_path / "a.txt").write_text("hi")
    steps = [_call("read", {"path": "a.txt"}, "c1"), _call("read", {"path": "nope.txt"}, "c2"),
             _call("bash", {"command": "rm -rf build"}, "c3")]

    def script(body):
        n = sum(1 for m in body["messages"] if m["role"] == "tool")
        if n < len(steps):
            return steps[n], _U
        return {"role": "assistant", "content": "done"}, _U

    with fake_server(script) as server:
        cfg = Config()
        m = ModelConfig(name="fake", model="fake", base_url=server.base_url, api_key="k")
        cfg.add_model(m, make_default=True)
        perms = PermissionManager()
        perms.set_prompt(lambda t, d: "no")
        agent = create_agent(model=m, config=cfg, cwd=str(tmp_path), permissions=perms,
                             stream=False, verbose=False)
        agent.run("look around")
    kinds = [e["kind"] for e in agent.session.events]
    assert kinds[0] == "turn" and kinds[-1] == "answer"
    tools = [e for e in agent.session.events if e["kind"] == "tool"]
    assert [(t["name"], t["error"], t["denied"]) for t in tools] == [
        ("read", False, False), ("read", True, False), ("bash", True, True)]
    assert all(isinstance(t["ms"], int) for t in tools) and "nope.txt" in tools[1]["detail"]
    stats = tool_stats(agent.session.events)
    assert stats["read"]["calls"] == 2 and stats["read"]["errors"] == 1
    assert stats["bash"]["denied"] == 1 and stats["bash"]["errors"] == 0
    assert [e["name"] for e in recent_problems(agent.session.events)] == ["read", "bash"]
    # Never part of what the model sees.
    assert all("events" not in m and "kind" not in m for m in agent.session.messages)


def test_saved_with_the_session_and_old_sessions_still_load(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    store = SessionStore()
    store.dir = tmp_path / "sessions"
    s = Session(system_prompt="sys")
    EventLog(s.events).add("tool", name="read", ms=3, error=False)
    store.save(s, session_id="abc", cwd=str(tmp_path), model="m")
    assert store.load("abc").events[0]["name"] == "read"
    data = json.loads((store.dir / "abc.json").read_text())
    data.pop("events")
    (store.dir / "old.json").write_text(json.dumps(data))
    assert store.load("old").events == []


def test_log_is_bounded_and_thread_safe():
    log = EventLog(cap=500)

    def spam():
        for _ in range(400):
            log.add("note", text="x" * 500)

    threads = [threading.Thread(target=spam) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(log.items) == 500 and len(log.items[-1]["text"]) == 200
