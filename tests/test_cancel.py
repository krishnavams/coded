"""Stopping at once where the turn runs off the main thread (the TUI): a slow
model call and a running shell command end on Esc, not at the next step."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from coded import shells
from coded.execstate import ExecState
from tests.test_agent_e2e import _make_agent

_U = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def _bash(cmd):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}]}


def _later(seconds, fn):
    t = threading.Timer(seconds, fn)
    t.start()
    return t


# -- shells.run --------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX sleep")
def test_a_cancelled_command_is_killed_at_once(tmp_path):
    cancel = threading.Event()
    marker = tmp_path / "child-alive"
    _later(0.3, cancel.set)
    start = time.monotonic()
    with pytest.raises(shells.CommandCancelled):
        shells.run(f"(sleep 2; touch {marker}) & sleep 30", cwd=str(tmp_path), cancel=cancel)
    assert time.monotonic() - start < 3
    time.sleep(2.5)
    assert not marker.exists()                  # the whole tree went, children too


@pytest.mark.skipif(os.name == "nt", reason="POSIX sleep")
def test_cancel_keeps_timeouts_input_and_output_working(tmp_path):
    cancel = threading.Event()
    done = shells.run("cat; echo done", cwd=str(tmp_path), input="hi\n", cancel=cancel)
    assert done.stdout == "hi\ndone\n" and done.returncode == 0
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        shells.run("sleep 30", cwd=str(tmp_path), timeout=0.5, cancel=cancel)
    assert time.monotonic() - start < 3


# -- the agent ----------------------------------------------------------------------

def test_a_slow_model_call_stops_on_interrupt(tmp_path, fake_server):
    def script(body):
        time.sleep(4)                           # an endpoint taking its time
        return {"role": "assistant", "content": "late"}, _U

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        agent.cancellable = True
        _later(0.4, agent.interrupt)
        start = time.monotonic()
        agent.run("explain the repo")
        took = time.monotonic() - start
        assert took < 2, took                   # not the 4 s the reply would take
        assert agent.session.messages[-1]["role"] == "user"   # nothing half-added
        assert any(e["kind"] == "interrupt" for e in agent.events.snapshot())


@pytest.mark.skipif(os.name == "nt", reason="POSIX sleep")
def test_a_running_shell_command_stops_on_interrupt(tmp_path, fake_server):
    calls = []

    def script(body):
        calls.append(body)
        return (_bash("sleep 30") if len(calls) == 1 else
                {"role": "assistant", "content": "never reached"}), _U

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        agent.cancellable = True
        _later(0.8, agent.interrupt)
        start = time.monotonic()
        agent.run("run the long thing")
        assert time.monotonic() - start < 4
    msgs = agent.session.messages
    assert msgs[-1]["role"] == "tool" and "Stopped: the user interrupted" in msgs[-1]["content"]
    assert len(calls) == 1                      # no further step after the stop


def test_without_cancellable_a_call_is_never_cut_short(tmp_path, fake_server):
    """The REPL (main thread, SIGINT) and --print keep the old path."""
    with fake_server(lambda b: ({"role": "assistant", "content": "ok"}, _U)) as server:
        agent = _make_agent(tmp_path, server.base_url)
        assert agent.cancellable is False and agent.run("hi") == "ok"


# -- the TUI -------------------------------------------------------------------------

pytest.importorskip("textual")


async def _esc_stops(tmp_path, fake_server, script, wait_before_esc):
    from coded.tui import CodedTUI
    from tests.test_tui import _agent

    with fake_server(script) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test() as pilot:
            app._submit("go")
            await pilot.pause(wait_before_esc)
            assert app.busy
            start = time.monotonic()
            await pilot.press("escape")
            while app.busy and time.monotonic() - start < 10:
                await pilot.pause(0.05)
            took = time.monotonic() - start
            assert not app.busy and app.execution.outcome is ExecState.INTERRUPTED
            return took, app


@pytest.mark.asyncio
async def test_tui_escape_cuts_a_slow_reply_short(tmp_path, fake_server):
    def script(body):
        time.sleep(5)
        return {"role": "assistant", "content": "late"}, _U

    took, app = await _esc_stops(tmp_path, fake_server, script, 0.4)
    assert took < 2, took


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX sleep")
async def test_tui_escape_kills_a_running_command(tmp_path, fake_server):
    calls = []

    def script(body):
        calls.append(1)
        return (_bash("sleep 30") if len(calls) == 1 else
                {"role": "assistant", "content": "x"}), _U

    took, app = await _esc_stops(tmp_path, fake_server, script, 1.0)
    assert took < 3, took
    assert len(calls) == 1                      # no step after the stop
