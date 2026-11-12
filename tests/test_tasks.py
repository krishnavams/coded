"""Sub-agents as tasks: progress, background runs, Ctrl+B, /tasks, Ctrl+O."""

from __future__ import annotations

import json
import threading
import time

from coded import ui
from coded.permissions import Decision, PermissionManager
from coded.tasks import TaskManager, format_elapsed, report_message
from tests.test_agent_e2e import _make_agent

SUB = "You are a focused sub-agent"
USAGE = {"prompt_tokens": 1, "completion_tokens": 1}


def _is_sub(body) -> bool:
    return str(body["messages"][0].get("content", "")).startswith(SUB)


def _call(name, args, cid="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


def _plain(renderable, width=100) -> str:
    from rich.console import Console

    con = Console(width=width, record=True, color_system=None)
    con.print(renderable)
    return con.export_text()


# -- the manager ----------------------------------------------------------------

def test_manager_tracks_activity_and_hands_over_background_reports():
    tm = TaskManager()
    fg = tm.start("search", "m")
    bg = tm.start("review api", "m", background=True)
    tm.record(bg, "Read(a.py)")
    tm.record(bg, "Search(foo)")
    assert [t.id for t in tm.running()] == [fg.id, bg.id]
    assert tm.foreground() is fg
    assert bg.summary().startswith("2 tool uses")
    finished = []
    tm.on_finish = finished.append
    tm.finish(fg, "fg report")              # foreground: no hand-over needed
    tm.finish(bg, "the report")
    assert finished == [bg] and not tm.running()
    taken = tm.take_finished()
    assert taken == [bg] and tm.take_finished() == []
    msg = report_message(taken)
    assert '[background task t2 "review api" completed' in msg and "the report" in msg
    assert format_elapsed(75) == "1m 15s" and format_elapsed(3700) == "1h 1m"


def test_a_background_thread_never_prompts():
    asked = []
    pm = PermissionManager(prompt=lambda t, d: asked.append(t) or "y",
                           rules={"allow": ["bash(ls*)"]})
    results = {}

    def worker():
        pm.set_background(lambda: True)
        results["risky"] = pm.request(key="bash", title="Run", detail="rm -rf x")
        results["allowed"] = pm.request(key="bash", title="Run", detail="ls -la")
        results["ask"] = pm.ask("t", "d")

    th = threading.Thread(target=worker)
    th.start()
    th.join()
    assert results == {"risky": Decision.DENY, "allowed": Decision.ALLOW, "ask": None}
    assert asked == []
    # The main thread still asks as before.
    assert pm.request(key="bash", title="Run", detail="rm x") is Decision.ALLOW
    assert asked == ["Run"]


# -- the agent ------------------------------------------------------------------

def test_sub_agent_activity_is_recorded_and_shown(tmp_path, fake_server):
    (tmp_path / "a.py").write_text("x = 1\n")

    def script(body):
        msgs = body["messages"]
        if _is_sub(body):
            if not any(m.get("role") == "tool" for m in msgs):
                return _call("read", {"path": "a.py"}, "s1"), USAGE
            return {"role": "assistant", "content": "a.py sets x"}, USAGE
        if not any(m.get("role") == "tool" for m in msgs):
            return _call("task", {"description": "look at a.py", "prompt": "read a.py"}), USAGE
        return {"role": "assistant", "content": "done"}, USAGE

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        assert agent.run("go") == "done"
    (t,) = agent.tasks.all()
    assert t.status == "completed" and t.label == "look at a.py"
    assert list(t.activity) == ["Read(a.py)"] and t.tool_uses == 1


def test_background_task_report_reaches_the_agent(tmp_path, fake_server):
    seen = []

    def script(body):
        msgs = body["messages"]
        if _is_sub(body):
            time.sleep(0.3)
            return {"role": "assistant", "content": "SUB REPORT: 3 issues"}, USAGE
        seen.append(msgs)
        if len(seen) == 1:
            return _call("task", {"description": "review api", "prompt": "review",
                                  "run_in_background": True}), USAGE
        if not any("SUB REPORT" in str(m.get("content")) for m in msgs):
            return {"role": "assistant", "content": "started it"}, USAGE
        return {"role": "assistant", "content": "summarised the 3 issues"}, USAGE

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        answer = agent.run("review in the background")
    # No front-end to wake it (wait_for_background): the turn waited for the report.
    assert answer == "summarised the 3 issues"
    tool_result = next(m for m in seen[1] if m.get("role") == "tool")
    assert "Started background task t1" in tool_result["content"]
    report = [m for m in seen[-1] if "[background task t1" in str(m.get("content"))]
    assert report and "SUB REPORT: 3 issues" in report[0]["content"]


def test_ctrl_b_moves_a_running_task_to_the_background(tmp_path, fake_server):
    release = threading.Event()

    def script(body):
        msgs = body["messages"]
        if _is_sub(body):
            release.wait(10)
            return {"role": "assistant", "content": "late report"}, USAGE
        if not any(m.get("role") == "tool" for m in msgs):
            return _call("task", {"description": "slow", "prompt": "be slow"}), USAGE
        return {"role": "assistant", "content": "ok"}, USAGE

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        agent.wait_for_background = False   # as in the REPL, which wakes the agent later
        agent.tasks.can_detach = True
        finished = []
        agent.tasks.on_finish = finished.append

        def press_ctrl_b():
            while agent.tasks.foreground() is None:
                time.sleep(0.02)
            agent.tasks.foreground().detach.set()

        threading.Thread(target=press_ctrl_b).start()
        assert agent.run("go") == "ok"            # the turn did not wait for it
        tool = next(m for m in agent.session.messages if m.get("role") == "tool")
        assert "moved this task to the background as t1" in tool["content"]
        release.set()
        agent.tasks.get("t1").done.wait(10)
    assert [t.id for t in finished] == ["t1"] and agent.tasks.has_undelivered()


def test_task_output_and_task_stop_tools(tmp_path):
    from coded.tools.base import ToolContext
    from coded.tools.task import TaskOutputTool, TaskStopTool

    tm = TaskManager()
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=None,
                      extra={"tasks": tm})
    t = tm.start("review", "m", background=True)
    tm.record(t, "Search(foo)")
    out = TaskOutputTool().run({"task_id": "t1"}, ctx).content
    assert "running" in out and "Search(foo)" in out

    class FakeAgent:
        stopped = False

        def interrupt(self):
            self.stopped = True

    t.agent = FakeAgent()
    agent = t.agent
    assert not TaskStopTool().run({"task_id": "t1"}, ctx).is_error
    assert agent.stopped
    tm.finish(t, "partial")
    assert t.status == "stopped"
    assert "partial" in TaskOutputTool().run({"task_id": "t1"}, ctx).content
    assert t.delivered  # read through the tool: not handed over again
    assert TaskStopTool().run({"task_id": "t9"}, ctx).is_error


# -- display --------------------------------------------------------------------

def test_the_dock_shows_running_tasks_with_their_latest_steps():
    tm = TaskManager()
    t = tm.start("review api suites", "qwen3-14b")
    for i in range(5):
        tm.record(t, f"Read(test_{i}.py)")
    collapsed = _plain(ui.running_tasks([t]))
    assert "Task(review api suites)" in collapsed and "qwen3-14b" in collapsed
    assert "5 tool uses" in collapsed and "Read(test_4.py)" in collapsed
    assert "Read(test_0.py)" not in collapsed and "+2 earlier (ctrl+o to expand)" in collapsed
    expanded = _plain(ui.running_tasks([t], expanded=True))
    assert "Read(test_0.py)" in expanded and "earlier" not in expanded
    dock = ui.TurnDock(lambda: "", get_running=tm.running, can_detach=lambda: True)
    text = _plain(dock.render())
    assert "Task(review api suites)" in text and "ctrl+b run task in background" in text


def test_ctrl_o_expands_and_collapses_tool_output(monkeypatch):
    from rich.console import Console

    con = Console(width=100, record=True, color_system=None)
    monkeypatch.setattr(ui, "console", con)
    monkeypatch.setattr(ui, "_last_cut", None)
    monkeypatch.setattr(ui, "_expanded", False)
    long = "\n".join(f"line {i}" for i in range(40))
    ui.tool_result(long, name="bash")
    out = con.export_text()
    assert "+28 lines (ctrl+o to expand)" in out and "line 39" not in out
    ui.set_expanded(True)
    assert ui.show_last_cut()
    assert "line 39" in con.export_text()
    ui.tool_result(long, name="bash")  # expanded: shown whole, no hint
    assert "line 39" in con.export_text() and "ctrl+o" not in con.export_text()
    ui.set_expanded(False)


def test_control_keys_reach_their_handlers_mid_turn():
    import io
    import os

    import pytest

    if os.name == "nt":
        pytest.skip("POSIX terminal reader")
    import pty

    from coded.typeahead import CTRL_B, CTRL_O, TypeAhead, _PosixKeys

    master, slave = pty.openpty()
    reader = _PosixKeys(fd=slave, out=io.StringIO())
    try:
        os.write(master, b"ab\x02cd\x0f")
        assert reader(0.5) == CTRL_B
        assert reader(0.5) == CTRL_O
        assert reader.buf == list("abcd")   # typing around the keys is kept
    finally:
        reader.close()
        os.close(master)
        os.close(slave)
    pressed = []
    keys = iter([CTRL_B, None])
    ta = TypeAhead(poll=lambda _t: next(keys, None), keys={CTRL_B: lambda: pressed.append("b")})
    with ta:
        time.sleep(0.2)
    assert pressed == ["b"] and ta.pending == 0


def test_repl_wakes_the_agent_with_a_finished_background_report(tmp_path, monkeypatch):
    from tests.test_progress_and_typeahead import _repl_agent, _repl_config

    from coded.repl import Repl

    agent = _repl_agent(tmp_path)
    repl = Repl(agent, _repl_config(tmp_path))
    assert agent.wait_for_background is False
    t = agent.tasks.start("review", "m", background=True)
    agent.tasks.finish(t, "REPORT BODY")
    ran = []
    monkeypatch.setattr(agent, "run", lambda text, images=None: ran.append(text) or "ok")
    monkeypatch.setattr("coded.repl.default_poll", lambda: lambda _t: None)
    monkeypatch.setattr(repl.prompt, "prompt", lambda *a, **k: (_ for _ in ()).throw(EOFError))
    repl.run()
    assert len(ran) == 1 and "REPORT BODY" in ran[0] and "[background task t1" in ran[0]
