"""One turn at a time per agent, one process per session, saves that never
corrupt, locks released on errors, stale locks of dead processes recovered."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from coded.agent import AgentBusy, create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.securefs import Busy, FileLock, pid_alive
from coded.session import Session
from coded.sessions_store import SessionKeeper, SessionStore, resume_command


def _agent(tmp_path, base_url):
    cfg = Config()
    model = ModelConfig(name="fake", model="fake", provider="openai-compatible",
                        base_url=base_url, api_key="k")
    cfg.add_model(model, make_default=True)
    return create_agent(model=model, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(auto_approve=True),
                        stream=False, verbose=False)


def test_two_sessions_run_side_by_side(tmp_path, fake_server):
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    def slow(body):
        time.sleep(0.3)
        return {"role": "assistant", "content": "ok"}, usage

    with fake_server(slow) as server:
        a, b = _agent(tmp_path, server.base_url), _agent(tmp_path, server.base_url)
        out = {}
        threads = [threading.Thread(target=lambda n=n, ag=ag: out.__setitem__(n, ag.run("hi")))
                   for n, ag in (("a", a), ("b", b))]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert out == {"a": "ok", "b": "ok"}
    assert time.monotonic() - started < 0.55  # really concurrent, not serialized


def test_a_second_turn_in_the_same_session_is_refused_not_interleaved(tmp_path, fake_server):
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    entered = threading.Event()
    release = threading.Event()

    def held(body):
        entered.set()
        release.wait(5)
        return {"role": "assistant", "content": "first"}, usage

    with fake_server(held) as server:
        agent = _agent(tmp_path, server.base_url)
        result = {}
        t = threading.Thread(target=lambda: result.__setitem__("r", agent.run("one")))
        t.start()
        assert entered.wait(5)
        with pytest.raises(AgentBusy):
            agent.run("two")
        release.set()
        t.join()
    assert result["r"] == "first"
    users = [m for m in agent.session.messages if m.get("role") == "user"]
    assert [m["content"] for m in users][-1] == "one"  # "two" never entered the history


def test_the_turn_lock_is_released_after_an_error_and_an_interrupt(tmp_path, fake_server,
                                                                   monkeypatch):
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    with fake_server(lambda body: ({"role": "assistant", "content": "ok"}, usage)) as server:
        agent = _agent(tmp_path, server.base_url)

        def boom(*a, **k):
            raise RuntimeError("model down")

        monkeypatch.setattr(agent, "_loop", boom)
        with pytest.raises(RuntimeError):
            agent.run("x")

        def interrupted(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(agent, "_loop", interrupted)
        with pytest.raises(KeyboardInterrupt):
            agent.run("y")
        monkeypatch.undo()
        assert agent.run("z") == "ok"


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_another_live_process_holding_a_session_blocks_resume(tmp_path, monkeypatch):
    import socket

    store = SessionStore()
    store.save(Session(system_prompt="s"), session_id="20260101-000000-001", cwd="/p",
               model="m")
    other = _sleeper()
    try:
        lock_path = store.lock("20260101-000000-001").path
        lock_path.write_text(json.dumps({"pid": other.pid, "host": socket.gethostname()}))
        keeper = SessionKeeper("/p", "m", store)
        agent = type("A", (), {})()
        agent.todos = type("T", (), {"items": []})()
        with pytest.raises(Busy) as exc:
            keeper.resume(agent, "20260101-000000-001")
        assert str(other.pid) in str(exc.value)
        msg, _table, resumed = resume_command(
            type("Ag", (), {"session": Session(system_prompt="s"), "todos": agent.todos})(),
            keeper, "20260101-000000-001")
        assert not resumed and "Cannot resume" in msg
    finally:
        other.kill()
        other.wait()
    # its owner is gone now: the stale lock is taken over
    assert not pid_alive(other.pid)
    keeper = SessionKeeper("/p", "m", store)
    agent = type("A", (), {})()
    agent.todos = type("T", (), {"items": []})()
    assert keeper.resume(agent, "20260101-000000-001") is not None
    assert json.loads(lock_path.read_text())["pid"] == os.getpid()
    keeper.release()
    assert not lock_path.exists()


def test_a_lock_is_released_when_the_work_raises(tmp_path):
    path = tmp_path / ".x.lock"
    with pytest.raises(ValueError):
        with FileLock(path):
            assert path.exists()
            raise ValueError("boom")
    assert not path.exists()
    with FileLock(path):  # free again
        pass


def test_a_lock_on_another_machine_is_respected(tmp_path):
    path = tmp_path / ".x.lock"
    path.write_text(json.dumps({"pid": 1, "host": "some-other-host"}))
    with pytest.raises(Busy):
        FileLock(path).acquire()


def test_concurrent_saves_never_leave_a_corrupt_session(tmp_path):
    store = SessionStore()
    errors = []

    def writer(n):
        for i in range(25):
            s = Session(system_prompt="s")
            s.messages.append({"role": "user", "content": f"{n}-{i}" * 200})
            try:
                store.save(s, session_id="20260101-000000-009", cwd="/p", model="m")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    data = json.loads((store.dir / "20260101-000000-009.json").read_text())
    assert data["messages"][-1]["role"] == "user"
    assert not [p for p in store.dir.iterdir() if p.name.endswith(".tmp")]


def test_a_mission_cannot_run_twice_at_once(tmp_path, monkeypatch):
    from coded import mission

    store = SessionStore()
    state = {"id": "20260101-000000", "goal": "g", "session_id": "20260101-000000-123",
             "work_dir": str(tmp_path), "root": str(tmp_path), "max_restarts": 1,
             "status": "running"}
    held = store.lock(state["session_id"]).acquire()
    try:
        # same process, another claim: refused like a second process would be
        args = mission.build_parser().parse_args(["--resume"])
        args.config = None
        args.no_mcp = True
        monkeypatch.setattr("coded.cli._select_model", lambda cfg, name: (
            ModelConfig(name="fake", model="fake", provider="openai-compatible",
                        base_url="http://127.0.0.1:9", api_key="k"), None))
        assert mission._run(state, args, resume=True) == 1
    finally:
        held.release()


def test_an_interrupted_worktree_task_leaves_no_orphan_worktree(tmp_path, monkeypatch):
    from coded.tools.base import ToolContext
    from coded.tools.task import _work

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@e", "commit",
                                  "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", *args], cwd=repo, check=True)

    def interrupted(prompt, **kw):
        raise KeyboardInterrupt

    ctx = ToolContext(cwd=str(repo), permissions=PermissionManager(), config=Config(),
                      spawn_subagent=interrupted)
    with pytest.raises(KeyboardInterrupt):
        _work({"prompt": "p", "isolation": "worktree"}, ctx, None)
    listed = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True,
                            text=True).stdout.strip().splitlines()
    assert len(listed) == 1  # only the main checkout
    branches = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=repo,
                              capture_output=True, text=True).stdout.split()
    assert not [b for b in branches if b.startswith("coded/")]
