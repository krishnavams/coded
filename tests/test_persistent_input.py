"""The input stays alive while work runs, and what is typed reaches the work.

Messages typed mid-turn are follow-ups for the running agent (delivered at its
next step, or before it would finish); commands run after it; read-only
commands answer at once; nothing typed is ever dropped — an interrupt hands
it back. Execution state is tracked apart from the input.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest

from coded import ui
from coded.agent import FOLLOWUP_HEADER
from coded.execstate import ExecState, Execution, badge
from coded.lineedit import LineEditor
from coded.typeahead import TypeAhead
from tests.test_agent_e2e import _make_agent

_U = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def _bash(cmd, cid="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}]}


def _has_followup(body, text):
    return any(m.get("role") == "user" and FOLLOWUP_HEADER in str(m.get("content"))
               and text in str(m.get("content")) for m in body["messages"])


# -- the agent ---------------------------------------------------------------------

def test_a_followup_reaches_the_agent_at_its_next_step(tmp_path, fake_server):
    bodies = []

    def script(body):
        bodies.append(body)
        if len(bodies) == 1:
            return _bash("echo working"), _U
        return {"role": "assistant", "content": "ok"}, _U

    inbox = []
    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        real = agent._run_tool_inner

        def tool_then_type(tc, display):
            out = real(tc, display)
            inbox.append("use port 8080")   # typed while the tool ran
            return out

        agent._run_tool_inner = tool_then_type
        agent.followups = lambda: [inbox.pop() for _ in range(len(inbox))]
        assert agent.run("build it") == "ok"
    assert len(bodies) == 2 and _has_followup(bodies[1], "use port 8080")
    roles = [m["role"] for m in bodies[1]["messages"]][-3:]
    assert roles == ["assistant", "tool", "user"]          # after the tool result: valid
    assert any(e["kind"] == "followup" for e in agent.events.snapshot())


def test_a_followup_typed_as_the_agent_finishes_is_answered_not_lost(tmp_path, fake_server):
    bodies = []
    inbox = []

    def script(body):
        bodies.append(body)
        if len(bodies) == 1:
            inbox.extend(["also add a test", "and update the README"])  # typed meanwhile
            return {"role": "assistant", "content": "first answer"}, _U
        return {"role": "assistant", "content": "did both"}, _U

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        agent.followups = lambda: [inbox.pop(0) for _ in range(len(inbox))]
        assert agent.run("fix the bug") == "did both"
    last = bodies[1]["messages"][-1]["content"]
    assert "1. also add a test" in last and "2. and update the README" in last


def test_a_broken_followup_source_never_breaks_the_turn(tmp_path, fake_server):
    with fake_server(lambda b: ({"role": "assistant", "content": "ok"}, _U)) as server:
        agent = _make_agent(tmp_path, server.base_url)
        agent.followups = lambda: 1 / 0
        assert agent.run("hi") == "ok"


# -- execution state ------------------------------------------------------------------

class _Busy:
    active_tools: list = []


def test_execution_states_follow_the_work():
    agent = _Busy()
    pending = []
    ex = Execution(agent, pending=lambda: len(pending))
    assert ex.state() is ExecState.IDLE
    ex.ready()
    assert ex.state() is ExecState.INPUT_READY
    ex.begin("build it")
    assert ex.state() is ExecState.COMMAND_RUNNING
    agent.active_tools = ["bash"]
    assert ex.state() is ExecState.TOOL_EXECUTING and "bash" in ex.detail()
    pending.append("x")
    assert "1 follow-up waiting" in ex.detail()
    agent.active_tools = []
    assert ex.state() is ExecState.FOLLOW_UP_PENDING
    with ex.waiting():
        assert ex.state() is ExecState.WAITING_FOR_USER_INPUT
    ex.interrupt()
    assert ex.state() is ExecState.INTERRUPTING
    assert ex.end(ok=False) is ExecState.INTERRUPTED and not ex.running
    ex.begin("again")
    assert ex.end(ok=True) is ExecState.COMPLETED
    ex.begin("boom")
    assert ex.end(ok=False, error="endpoint down") is ExecState.FAILED
    assert "endpoint down" in ex.detail() and badge(ex).plain.startswith("✗ failed")
    states = [s for _, s, _ in ex.history]
    assert ExecState.INTERRUPTING in states and states[-1] is ExecState.FAILED


# -- routing in the reader ----------------------------------------------------------------

class _Keys:
    def __init__(self):
        self.editor, self.echo = LineEditor(), False

    def __call__(self, _t):
        return None


def test_messages_go_to_the_agent_commands_wait():
    ta = TypeAhead(poll=_Keys())
    for line in ("also check tests", "/review", "and lint"):
        ta.feed(line)
    assert ta.followups_waiting == 2
    assert ta.take_followups() == ["also check tests", "and lint"]
    assert ta.queued_items == ["/review"] and ta.take_followups() == []


def test_history_up_down_and_search_mid_turn():
    keys = _Keys()
    ta = TypeAhead(poll=keys, history=lambda: ["run tests", "deploy staging", "run lint"])
    keys.editor.set_text("draft")
    assert ta.up() == "run lint" and ta.up() == "deploy staging"
    assert ta.down() == "run lint"
    assert ta.down() == "draft" and ta.typing == "draft"      # back to what was typed
    ta.feed("queued msg")
    assert ta.up() == "queued msg"                           # a queued message comes first
    keys.editor.set_text("run")
    assert ta.search_history() == "run lint"                  # Ctrl+R: newest match
    assert ta.search_history() == "run tests"                 # again: older
    assert ta.search_history() is None and ta.typing == "run tests"


# -- the REPL --------------------------------------------------------------------------

@pytest.fixture
def repl(tmp_path, fake_server, monkeypatch, headless):
    from coded.config import Config
    from coded.repl import Repl

    con = ui.TranscriptConsole(width=120, record=True, file=io.StringIO(), color_system=None)
    monkeypatch.setattr(ui, "console", con)
    cmds = tmp_path / ".coded" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "ship.md").write_text("---\ndescription: ship it\n---\nShip $ARGUMENTS\n")
    with fake_server(lambda b: ({"role": "assistant", "content": "ok"}, _U)) as server:
        from coded.skills import discover_skills

        agent = _make_agent(tmp_path, server.base_url)
        agent.skills = discover_skills(str(tmp_path))   # planner, reviewer, qa, …
        r = Repl(agent, Config())
        monkeypatch.setattr(r, "_read_line", lambda label: None)
        yield r


def test_commands_that_run_the_agent_keep_the_input_alive(repl, monkeypatch):
    seen = {}

    def run(msg, **kw):
        seen["busy"] = repl._typeahead is not None and repl.execution.running
        seen["followups"] = repl.agent.followups is not None
        return "ok"

    monkeypatch.setattr(repl.agent, "run", run)
    for line in ("/ship now", "/init", "/plan think about it", "/skill planner do x"):
        seen.clear()
        repl._handle_slash(line)
        assert seen == {"busy": True, "followups": True}, line
        repl.agent.set_mode("default", announce=False)
    assert repl._typeahead is None and repl.agent.followups is None
    assert repl.execution.outcome is ExecState.COMPLETED


def test_long_commands_without_the_agent_loop_still_keep_the_input(repl, monkeypatch):
    import coded.review as review

    seen = {}
    monkeypatch.setattr(review, "run_review", lambda *a, **k: seen.setdefault(
        "busy", (repl._typeahead is not None, repl.agent.followups)))
    repl._handle_slash("/review")
    assert seen["busy"] == (True, None)       # input alive; messages wait for the end


def test_read_only_commands_answer_mid_turn(repl):
    assert repl._intercept_midturn("/cost") is True
    assert repl._intercept_midturn("/review") is False      # waits for the turn
    assert repl._intercept_midturn("go on") is False
    assert "Input tokens" in ui.console.export_text()


def test_an_interrupt_hands_typed_messages_back_instead_of_dropping_them(repl):
    with pytest.raises(KeyboardInterrupt):
        with repl._busy("build it"):
            repl._typeahead.feed("/review")
            repl._typeahead.feed("then deploy")
            raise KeyboardInterrupt
    repl._keep_after_interrupt()
    assert repl._backlog == []
    assert repl._prefill == "/review\nthen deploy"                # in the input, not run
    assert repl.execution.outcome is ExecState.INTERRUPTED
    assert "Kept 2 message(s)" in ui.console.export_text()


def test_messages_left_after_a_normal_end_run_next(repl):
    with repl._busy("work", kind="command"):
        repl._typeahead.feed("summarize")
    assert repl._backlog == ["summarize"]


# -- the TUI ---------------------------------------------------------------------------

pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_tui_followups_reach_the_running_turn(tmp_path, fake_server):
    from textual.widgets import Input

    from coded.tui import CodedTUI
    from tests.test_tui import _agent

    bodies = []

    def script(body):
        bodies.append(body)
        if len(bodies) == 1:
            return _bash("sleep 1"), _U
        return {"role": "assistant", "content": "ok"}, _U

    with fake_server(script) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)
            app._submit("build it")
            await pilot.pause(0.3)
            assert app.busy and prompt.disabled is False          # the input stays usable
            prompt.value = "use port 8080"
            await pilot.press("enter")
            await pilot.pause()
            assert app.execution.state() in (ExecState.TOOL_EXECUTING,
                                             ExecState.FOLLOW_UP_PENDING)
            await app.workers.wait_for_complete()
            await pilot.pause()
    assert _has_followup(bodies[1], "use port 8080")
    assert app.execution.outcome is ExecState.COMPLETED and app._queued == []


@pytest.mark.asyncio
async def test_tui_escape_interrupts_and_keeps_the_queue(tmp_path, fake_server):
    from textual.widgets import Input

    from coded.tui import CodedTUI
    from tests.test_tui import _agent

    calls = []

    def script(body):
        calls.append(1)
        return _bash(f"sleep 1 # {len(calls)}", cid=f"c{len(calls)}"), _U   # never finishes

    with fake_server(script) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)
            app._submit("loop forever")
            await pilot.pause(0.3)
            prompt.value = "/timeline"
            await pilot.press("enter")                 # a command: waits for the turn
            await pilot.press("escape")                # no dropdown open: stop
            await pilot.pause()
            assert app.execution.state() is ExecState.INTERRUPTING
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not app.busy and app.execution.outcome is ExecState.INTERRUPTED
            assert prompt.value == "/timeline"          # handed back, not run, not lost
            assert len(calls) <= 2


# -- the real REPL in a terminal ---------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX pty")
def test_real_repl_delivers_a_followup_typed_while_a_tool_runs(tmp_path):
    import pty
    import re
    import select
    import subprocess

    from tests.conftest import FakeServer

    bodies = []

    def script(body):
        bodies.append(body)
        if len(bodies) == 1:
            return _bash("sleep 2; echo built"), None
        if any(FOLLOWUP_HEADER in str(m.get("content")) for m in body["messages"]):
            return {"role": "assistant", "content": "Used port 8080."}, None
        return {"role": "assistant", "content": "Done."}, None

    def read(fd, secs):
        out, end = b"", time.time() + secs
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    break
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.decode("utf-8", "replace"))

    with FakeServer(script) as srv:
        master, slave = pty.openpty()
        env = dict(os.environ, CODED_NO_UPDATE_CHECK="1", TERM="xterm-256color",
                   HOME=str(tmp_path))
        proc = subprocess.Popen(
            [sys.executable, "-m", "coded", "--base-url", srv.base_url, "--model-name", "fake",
             "--api-key", "k", "--yolo"], stdin=slave, stdout=slave, stderr=slave,
            cwd=str(tmp_path), env=env, close_fds=True)
        try:
            read(master, 4)
            os.write(master, b"build the app\r")
            read(master, 1.0)                    # the shell tool is running now
            os.write(master, b"use port 8080\r")
            screen = read(master, 5)
        finally:
            os.write(master, b"\x04")
            proc.kill()
            os.close(master)
            os.close(slave)
    assert len(bodies) == 2 and _has_followup(bodies[1], "use port 8080")
    assert "follow-up delivered" in screen and "Used port 8080." in screen
