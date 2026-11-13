"""Self-improvement lab: recurring failures in past sessions, proposals applied
only one by one with approval, loosening ones flagged."""

from __future__ import annotations

import io
import json

from coded import lab
from coded.session import Session
from coded.sessions_store import SessionStore


def _store(tmp_path):
    store = SessionStore.__new__(SessionStore)
    store.dir = tmp_path / "sessions"
    return store


def _save(store, sid, cwd, events, messages=None):
    s = Session(system_prompt="sys")
    s.events = events
    if messages:
        s.messages += messages
    store.save(s, session_id=sid, cwd=str(cwd), model="m")


def _fail(detail):
    return {"t": 1, "kind": "tool", "name": "bash", "error": True, "denied": False,
            "detail": detail}


def _sessions(tmp_path):
    store = _store(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    call = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "grep", "arguments": '{"pattern": "a"}'}}]}
    _save(store, "s1", proj, [
        {"t": 1, "kind": "turn", "prompt": "fix"},
        _fail("[exit 127] /usr/bin/env: 'node': No such file"),
        _fail("[exit 127] /opt/x/env: 'node': No such file"),
        {"t": 2, "kind": "tool", "name": "delete", "error": True, "denied": True,
         "reason": "user"},
        {"t": 2, "kind": "tool", "name": "write", "error": True, "denied": True,
         "reason": "plan"},
        {"t": 2, "kind": "tool", "name": "write", "error": True, "denied": True,
         "reason": "plan"},
        {"t": 2, "kind": "tool", "name": "write", "error": True, "denied": True,
         "reason": "plan"},
        {"t": 3, "kind": "verify", "result": "stall", "command": "pytest -q"},
        {"t": 4, "kind": "max_turns", "limit": 50},
    ], messages=[call] * 5)
    _save(store, "s2", proj, [
        {"t": 5, "kind": "turn", "prompt": "again"},
        _fail("[exit 127] /usr/local/env: 'node': No such file"),
        {"t": 6, "kind": "tool", "name": "delete", "error": True, "denied": True,
         "reason": "user"},
        {"t": 6, "kind": "tool", "name": "delete", "error": True, "denied": True,
         "reason": "user"},
        {"t": 7, "kind": "verify", "result": "stall", "command": "pytest -q"},
    ])
    other = tmp_path / "other"
    other.mkdir()
    _save(store, "s3", other, [_fail("unrelated")] * 9)
    return store, proj


def test_finds_what_keeps_going_wrong(tmp_path):
    store, proj = _sessions(tmp_path)
    sessions = lab.load_sessions(str(proj), store=store)
    assert len(sessions) == 2  # the other folder's session is not read
    report = lab.analyze(sessions)
    titles = [f.title for f in report.findings]
    assert "`bash` keeps failing the same way" in titles
    assert "You denied `delete` 3 times" in titles
    assert not any("`write`" in t for t in titles)  # plan-mode refusals are not "you"
    assert "The checks fail the same way after a fix" in titles
    assert "Turns hit max_turns" in titles
    assert any("repeated the same `grep` call 5 times" in t for t in titles)
    cap = next(f for f in report.findings if f.title == "Turns hit max_turns")
    assert cap.proposal.loosens and cap.proposal.value == {"max_turns": 100}
    from rich.console import Console

    con = Console(width=120, record=True, file=io.StringIO(), color_system=None)
    con.print(lab.render(report))
    out = con.export_text()
    assert "Self-improvement lab" in out and "lets the agent do more" in out
    assert "Nothing changes until" in out


def test_applies_only_what_is_approved_one_by_one(tmp_path):
    store, proj = _sessions(tmp_path)
    report = lab.analyze(lab.load_sessions(str(proj), store=store))
    asked, said = [], []
    answers = iter(["y", "n", "y", "n", "n", "n"])

    def ask(q):
        asked.append(q)
        return next(answers, "n")

    n = lab.apply_interactive(str(proj), report, ask, said.append)
    proposals = [f for f in report.findings if f.proposal]
    assert len(asked) == len(proposals) and n == 2
    assert any("lets the agent do more" in s for s in said)
    from coded.memory import MemoryStore

    assert not (proj / "CODED.md").exists()  # committed files are never touched
    notes = MemoryStore.for_project(str(proj)).load()
    assert notes and notes[0].kind == "gotcha" and notes[0].source == "user"
    cfg = proj / ".coded" / "config.json"
    assert not cfg.exists() or "max_turns" not in json.loads(cfg.read_text())


def test_cli_apply_needs_a_terminal(tmp_path, monkeypatch):
    from coded import cli

    monkeypatch.setattr(lab, "load_sessions", lambda cwd, store=None: [])
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    assert cli._handle_lab_command(["--cwd", str(tmp_path)]) == 0
    assert cli._handle_lab_command(["--cwd", str(tmp_path), "--apply"]) == 2
