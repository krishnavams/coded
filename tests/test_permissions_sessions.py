"""Tests for permission allow/deny rules, diff preview, and session persistence."""

from __future__ import annotations

from coded.permissions import Decision, PermissionManager
from coded.session import Session
from coded.tools import build_registry
from coded.tools.base import ToolContext


# -- permission rules -------------------------------------------------------
def test_allow_rule_skips_prompt():
    pm = PermissionManager(prompt=lambda t, d: "no", rules={"allow": ["bash(git *)"]})
    assert pm.request(key="bash", title="x", detail="git status", target="git status") is Decision.ALLOW
    # A command not matching the allow rule still prompts (and here is denied).
    assert pm.request(key="bash", title="x", detail="rm -rf /", target="rm -rf /") is Decision.DENY


def test_deny_rule_beats_auto_approve():
    pm = PermissionManager(auto_approve=True, rules={"deny": ["bash(rm *)"]})
    assert pm.request(key="bash", title="x", detail="rm -rf x", target="rm -rf x") is Decision.DENY
    # Non-denied commands are still auto-approved.
    assert pm.request(key="bash", title="x", detail="ls", target="ls") is Decision.ALLOW


def test_bare_tool_rule_matches_any_target():
    pm = PermissionManager(prompt=lambda t, d: "no", rules={"allow": ["write"]})
    assert pm.request(key="write", title="x", detail="Write", target="/any/path") is Decision.ALLOW


def test_wildcard_rule():
    pm = PermissionManager(auto_approve=True, rules={"deny": ["*"]})
    assert pm.request(key="bash", title="x", detail="ls", target="ls") is Decision.DENY


# -- diff preview -----------------------------------------------------------
def test_edit_preview_produces_diff(tmp_path):
    (tmp_path / "f.txt").write_text("hello world\n")
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=None)
    tool = build_registry().get("edit")
    diff = tool.preview({"path": "f.txt", "old_string": "world", "new_string": "there"}, ctx)
    assert diff is not None
    assert "-hello world" in diff and "+hello there" in diff


def test_write_preview_new_file(tmp_path):
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=None)
    tool = build_registry().get("write")
    diff = tool.preview({"path": "new.txt", "content": "line1\nline2\n"}, ctx)
    assert "+line1" in diff and "+line2" in diff


# -- session persistence ----------------------------------------------------
def test_session_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setenv("CODED_SESSIONS_DIR", str(tmp_path / "sessions"))
    from coded.sessions_store import SessionStore

    store = SessionStore()
    s = Session(system_prompt="SYS")
    s.add_user("hello")
    s.add_assistant("hi there")
    s.turns = 1

    sid = store.new_id()
    store.save(s, session_id=sid, cwd="/proj", model="gpt-4o")

    loaded = store.load(sid)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "SYS"
    assert any(m.get("content") == "hello" for m in loaded.messages)
    assert loaded.turns == 1


def test_print_json_output(tmp_path, monkeypatch, capsys, fake_server):
    """--print --output-format json emits clean parseable JSON."""
    import json as _json
    from coded.cli import main

    monkeypatch.setenv("CODED_SESSIONS_DIR", str(tmp_path / "sessions"))

    def script(body):
        return ({"role": "assistant", "content": "hello result"},
                {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16})

    with fake_server(script) as server:
        code = main(["--print", "--output-format", "json", "--cwd", str(tmp_path),
                     "--base-url", server.base_url, "--api-key", "k", "--model-name", "fake",
                     "say hi"])
    assert code == 0
    out = capsys.readouterr().out
    data = _json.loads(out)  # must be valid JSON with nothing else on stdout
    assert data["content"] == "hello result"
    assert data["usage"]["input_tokens"] == 12
    assert data["usage"]["output_tokens"] == 4


def test_session_list_and_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("CODED_SESSIONS_DIR", str(tmp_path / "sessions"))
    from coded.sessions_store import SessionStore

    store = SessionStore()
    s = Session(system_prompt="SYS")
    s.add_user("x")
    id1 = store.new_id()
    store.save(s, session_id=id1, cwd="/proj-a", model="m")
    import time
    time.sleep(0.01)
    id2 = store.new_id()
    store.save(s, session_id=id2, cwd="/proj-b", model="m")

    metas = store.list()
    assert {m.id for m in metas} == {id1, id2}
    assert store.latest() == id2  # most recently updated
    assert store.latest("/proj-a") == id1
