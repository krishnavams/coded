"""Waiting on long work is not a loop, and need not burn steps.

Polling a background shell or task that is still running counts as progress
(`call_record`); `bash_output` / `task_output` take `wait` to block until there
is something new instead of being called again and again.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from coded.agent import call_record, stuck_reason
from coded.background import BackgroundShells
from coded.config import Config
from coded.permissions import PermissionManager
from coded.tools.base import ToolContext
from coded.tools.shell import BashOutputTool
from coded.tools.task import TaskOutputTool

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX shell commands")


def _ctx(tmp_path, **extra):
    return ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=Config(),
                       extra=extra)


# -- the loop check ----------------------------------------------------------------------

def test_polling_something_still_running_is_never_a_loop():
    args = '{"shell_id": "bg1"}'
    running = [call_record("bash_output", args, False, "[running]\n(no new output)")] * 10
    assert stuck_reason([call_record("bash_output", args, False, "[running]\nx")
                         for _ in range(10)]) is None
    assert len({r[3] for r in running}) == 1          # (the same object, repeated)
    task = [call_record("task_output", '{"task_id": "t1"}', False,
                        't1 "build": running — 3 tool uses · 20s') for _ in range(10)]
    assert stuck_reason(task) is None


def test_a_finished_shell_read_again_and_again_is_a_loop():
    args = '{"shell_id": "bg1"}'
    same = [call_record("bash_output", args, False, "[exited 0]\n(no new output)")] * 4
    assert "same `bash_output` call 4 times" in stuck_reason(same)
    # Other tools keep the old rule: same name + arguments = the same call.
    reads = [call_record("read", '{"path": "a"}', False, f"different {i}") for i in range(4)]
    assert "same `read` call 4 times" in stuck_reason(reads)
    assert stuck_reason([("read", '{"path": "a"}', False)] * 4)   # old 3-tuples still work


@POSIX
def test_an_agent_waiting_on_a_long_command_is_given_more_steps(tmp_path, fake_server):
    """The case from the field: a mission stopped as 'not progressing' while it
    was polling a background job."""
    from tests.test_agent_e2e import _make_agent

    calls = []

    def call(name, args, cid):
        return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}

    def script(body):
        calls.append(1)
        n = len(calls)
        if n == 1:
            return call("bash", {"command": "sleep 2; echo done", "run_in_background": True},
                        "c1"), None
        text = json.dumps(body["messages"])
        if "exited 0" not in text and n < 40:
            time.sleep(0.15)
            return call("bash_output", {"shell_id": "bg1"}, f"c{n}"), None
        return {"role": "assistant", "content": "The build finished."}, None

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url, auto_approve=True)
        agent.config.max_turns = 4                         # polling outlasts the limit
        assert agent.run("build it") == "The build finished."
    kinds = [e["kind"] for e in agent.events.snapshot()]
    assert "turns_extended" in kinds and "max_turns" not in kinds


# -- waiting instead of polling ----------------------------------------------------------

@POSIX
def test_bash_output_wait_returns_on_new_output_not_after_the_whole_wait(tmp_path):
    shells = BackgroundShells()
    sid = shells.start("sleep 0.5; echo hello; sleep 30", str(tmp_path), "slow")
    try:
        start = time.monotonic()
        out = BashOutputTool().run({"shell_id": sid, "wait": 20}, _ctx(tmp_path, shells=shells))
        took = time.monotonic() - start
        assert "hello" in out.content and out.content.startswith("[running]")
        assert 0.3 < took < 5, took
    finally:
        shells.kill_all()


@POSIX
def test_bash_output_wait_ends_at_exit_timeout_or_cancel(tmp_path):
    shells = BackgroundShells()
    quick = shells.start("true", str(tmp_path), "quick")
    hang = shells.start("sleep 30", str(tmp_path), "hang")
    tool = BashOutputTool()
    try:
        time.sleep(0.3)
        tool.run({"shell_id": quick}, _ctx(tmp_path, shells=shells))       # read it all
        start = time.monotonic()
        assert "exited 0" in tool.run({"shell_id": quick, "wait": 30},
                                      _ctx(tmp_path, shells=shells)).content
        assert time.monotonic() - start < 1                                  # already done
        start = time.monotonic()
        tool.run({"shell_id": hang, "wait": 0.6}, _ctx(tmp_path, shells=shells))
        assert 0.5 < time.monotonic() - start < 3                            # the timeout
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        start = time.monotonic()
        tool.run({"shell_id": hang, "wait": 30}, _ctx(tmp_path, shells=shells, cancel=cancel))
        assert time.monotonic() - start < 3                                  # Esc stops it
        assert tool.run({"shell_id": hang, "wait": "soon"},                  # bad value: no wait
                        _ctx(tmp_path, shells=shells)).content.startswith("[running]")
    finally:
        shells.kill_all()


def test_task_output_wait_returns_when_the_task_finishes(tmp_path):
    from coded.tasks import TaskManager

    tm = TaskManager()
    entry = tm.start("long job", "m", background=True)
    threading.Timer(0.4, lambda: tm.finish(entry, "all done", "completed")).start()
    start = time.monotonic()
    out = TaskOutputTool().run({"task_id": entry.id, "wait": 20}, _ctx(tmp_path, tasks=tm))
    assert "all done" in out.content and time.monotonic() - start < 5
    running = tm.start("forever", "m", background=True)
    start = time.monotonic()
    out = TaskOutputTool().run({"task_id": running.id, "wait": 0.5}, _ctx(tmp_path, tasks=tm))
    assert "running" in out.content and 0.4 < time.monotonic() - start < 3
