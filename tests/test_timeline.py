"""Time-travel: the timeline, a turn replayed with its file changes, restore
with a preview, and a saved session replayed."""

from __future__ import annotations

import io
import json

from coded import timeline
from coded.agent import create_agent
from coded.checkpoints import CheckpointManager
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager

_U = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def _plain(renderable) -> str:
    from rich.console import Console

    con = Console(width=120, record=True, file=io.StringIO(), color_system=None)
    con.print(renderable)
    return con.export_text()


def _write(path, content, cid):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
            "function": {"name": "write", "arguments": json.dumps(
                {"path": path, "content": content})}}]}


def _two_turns(tmp_path, fake_server):
    (tmp_path / "app.py").write_text("v = 0\n")
    plan = {"first": "v = 1\n", "second": "v = 2\n"}

    def script(body):
        last_user = next(m for m in reversed(body["messages"]) if m["role"] == "user")
        if body["messages"][-1]["role"] == "tool":
            return {"role": "assistant", "content": "Updated."}, _U
        word = str(last_user["content"]).split()[0]
        return _write("app.py", plan[word], f"w-{word}"), _U

    with fake_server(script) as server:
        cfg = Config()
        m = ModelConfig(name="fake", model="fake", base_url=server.base_url, api_key="k")
        cfg.add_model(m, make_default=True)
        agent = create_agent(model=m, config=cfg, cwd=str(tmp_path),
                             permissions=PermissionManager(auto_approve=True),
                             stream=False, verbose=False)
        agent.checkpoints = CheckpointManager(str(tmp_path))
        agent.run("first change")
        agent.run("second change")
    return agent


def test_timeline_lists_and_replays_turns_with_their_file_changes(tmp_path, fake_server):
    agent = _two_turns(tmp_path, fake_server)
    turns = timeline.build_for(agent)
    assert [t.prompt for t in turns] == ["first change", "second change"]
    tool = next(s for s in turns[0].steps if s.kind == "tool")
    assert tool.name == "write" and tool.event and tool.event["error"] is False
    # Turn 1 changed v = 0 → 1 (its "after" is the backup turn 2 took).
    (path, before, after), = timeline.turn_changes(agent.checkpoints, turns[0].checkpoint)
    assert (before, after) == ("v = 0\n", "v = 1\n")
    (_, before2, after2), = timeline.turn_changes(agent.checkpoints, turns[1].checkpoint)
    assert (before2, after2) == ("v = 1\n", "v = 2\n")
    text = _plain(timeline.render_turn(turns[0], agent.checkpoints, agent.cwd))
    assert "first change" in text and "Write(app.py)" in text and "ok" in text
    assert "- v = 0" in text and "+ v = 1" in text and "bash edits are not snapshotted" in text
    assert "second change" in _plain(timeline.render_list(turns))


def test_restore_previews_then_goes_back_through_rewind(tmp_path, fake_server, headless,
                                                         monkeypatch):
    from rich.console import Console

    from coded import ui
    from coded.repl import Repl

    agent = _two_turns(tmp_path, fake_server)
    turns = timeline.build_for(agent)
    preview = timeline.restore_preview(agent.checkpoints, turns[0].checkpoint)
    assert [(now, target) for _, now, target in preview] == [("v = 2\n", "v = 0\n")]
    repl = Repl(agent, agent.config)
    con = Console(width=120, record=True, file=io.StringIO(), color_system=None)
    monkeypatch.setattr(ui, "console", con)
    agent.permissions.set_prompt(lambda t, d: "no")
    repl._handle_slash("/timeline restore 1")
    assert (tmp_path / "app.py").read_text() == "v = 2\n"  # declined: nothing changed
    assert "would change 1 file" in con.export_text()
    agent.permissions.set_prompt(lambda t, d: "yes")
    repl._handle_slash("/timeline restore 1")
    assert (tmp_path / "app.py").read_text() == "v = 0\n"
    assert agent.turn_history() == []


def test_saved_session_replays_without_files(tmp_path, fake_server, monkeypatch, capsys):
    from coded import cli
    from coded.sessions_store import SessionStore

    agent = _two_turns(tmp_path, fake_server)
    store = SessionStore()
    monkeypatch.setattr(SessionStore, "__init__", lambda self: setattr(self, "dir", tmp_path / "s"))
    store = SessionStore()
    store.save(agent.session, session_id="sess1", cwd=str(tmp_path), model="fake")
    turns = timeline.build(store.load("sess1").messages, store.load("sess1").events)
    assert [t.prompt for t in turns] == ["first change", "second change"]
    assert turns[1].steps[0].event["name"] == "write"  # outcomes survive the save
    assert cli._sessions_show(["sess1"]) == 0
    assert cli._sessions_show(["sess1", "9"]) == 2
