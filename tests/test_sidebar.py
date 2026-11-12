"""The TUI's right-hand panel: what it shows, and when it is open."""

from __future__ import annotations

import json
import sys

import pytest

from coded import sidebar
from coded.agent import create_agent
from coded.checkpoints import CheckpointManager
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager


def _agent(tmp_path, base_url="http://127.0.0.1:9/v1", window=128_000):
    cfg = Config()
    model = ModelConfig(name="fake", model="fake", base_url=base_url, api_key="k",
                        context_window=window)
    cfg.add_model(model, make_default=True)
    return create_agent(model=model, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(auto_approve=True),
                        stream=False, verbose=False,
                        checkpoints=CheckpointManager(str(tmp_path)))


# -- file changes ----------------------------------------------------------------

def test_changed_files_are_counted_against_their_snapshot(tmp_path):
    cp = CheckpointManager(str(tmp_path))
    cp.begin("turn")
    edited, created, gone, same = (tmp_path / n for n in ("a.py", "new.txt", "old.txt", "same.txt"))
    edited.write_text("one\ntwo\nthree\n")
    gone.write_text("bye\n")
    same.write_text("x\n")
    for p in (edited, created, gone, same):
        cp.record(p)
    edited.write_text("one\nTWO\nthree\nfour\n")
    created.write_text("a\nb\n")
    gone.unlink()
    same.write_text("x\n")          # written back unchanged: not a change

    changes = {c.path: c for c in sidebar.file_changes(cp, str(tmp_path))}
    assert set(changes) == {"a.py", "new.txt", "old.txt"}
    assert (changes["a.py"].added, changes["a.py"].removed, changes["a.py"].status) == (2, 1, "modified")
    assert (changes["new.txt"].added, changes["new.txt"].status) == (2, "new")
    assert changes["old.txt"].status == "deleted"


def test_no_checkpoint_group_means_no_changes(tmp_path):
    assert sidebar.file_changes(CheckpointManager(str(tmp_path)), str(tmp_path)) == []
    assert sidebar.file_changes(None, str(tmp_path)) == []


def test_a_binary_file_is_listed_without_counts(tmp_path):
    cp = CheckpointManager(str(tmp_path))
    blob = tmp_path / "img.bin"
    cp.record(blob)
    blob.write_bytes(b"\0\1\2" * 10)
    [change] = sidebar.file_changes(cp, str(tmp_path))
    assert change.added is None and change.status == "new"


# -- rendering -------------------------------------------------------------------

def test_the_panel_shows_each_section_only_when_it_has_something(tmp_path):
    agent = _agent(tmp_path)
    bare = sidebar.render(agent).plain
    assert "Session" in bare and "fake" in bare and "ctx" in bare
    for absent in ("Tasks", "Changed", "Queued", "Shells"):
        assert absent not in bare

    agent.todos.items = [{"content": "Read the code", "status": "completed"},
                         {"content": "Write the panel", "status": "in_progress"},
                         {"content": "Test it", "status": "pending"}]
    changes = [sidebar.FileChange("coded/tui.py", 40, 3, "modified"),
               sidebar.FileChange("coded/sidebar.py", 120, 0, "new")]
    full = sidebar.render(agent, busy=True, queued=["and add a test"], changes=changes).plain
    assert "Tasks  1/3" in full and "◐ Write the panel" in full
    assert "Changed this turn  2" in full
    assert "+40 −3" in full and "new +120" in full
    assert "Queued  1" in full and "1. and add a test" in full
    assert "working…" in full


def test_after_the_turn_the_files_are_labelled_as_last_turns(tmp_path):
    text = sidebar.render(_agent(tmp_path), busy=False,
                          changes=[sidebar.FileChange("a.py", 1, 0, "modified")]).plain
    assert "Changed last turn" in text


def test_context_fill_follows_the_window_and_warns_past_compaction(tmp_path):
    agent = _agent(tmp_path, window=10_000)
    agent.session.last_prompt_tokens = 2_500
    used, window, compact_at = sidebar.context_fill(agent)
    assert (used, window) == (2_500, 10_000) and compact_at < window
    assert "25%" in sidebar.render(agent).plain
    assert "2.5k / 10.0k" in sidebar.render(agent).plain


def test_background_shells_are_listed_with_their_state(tmp_path):
    agent = _agent(tmp_path)
    cmd = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    shell_id = agent.shells.start(cmd, str(tmp_path), "python sleeper")
    try:
        text = sidebar.render(agent).plain
        assert "Shells  1 running" in text and shell_id in text and "python sleeper" in text
    finally:
        agent.shells.kill_all()


def test_long_lines_never_overflow_the_panel(tmp_path):
    agent = _agent(tmp_path)
    agent.todos.items = [{"content": "x" * 200, "status": "pending"}]
    text = sidebar.render(agent, queued=["y" * 200],
                          changes=[sidebar.FileChange("deep/" * 30 + "f.py", 1, 1, "modified")])
    assert max(len(line) for line in text.plain.splitlines()) <= sidebar.PANEL_WIDTH - 3  # border + padding


# -- in the TUI ------------------------------------------------------------------

pytest.importorskip("textual")


def _script(body):
    usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    if any(m.get("role") == "tool" for m in body.get("messages", [])):
        return {"role": "assistant", "content": "Done."}, usage
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": "c1", "type": "function", "function": {
            "name": "write", "arguments": json.dumps({"path": "hello.py",
                                                      "content": "print('hi')\nprint('bye')\n"})}}]}, usage


@pytest.mark.asyncio
async def test_the_panel_opens_on_a_wide_terminal_and_stays_shut_on_a_narrow_one(tmp_path):
    from coded.tui import CodedTUI

    wide = CodedTUI(_agent(tmp_path))
    async with wide.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert wide.panel_open and wide.query_one("#sidebar").has_class("open")

    narrow = CodedTUI(_agent(tmp_path))
    async with narrow.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert not narrow.panel_open
        await pilot.press("ctrl+b")          # asked for: opens even when narrow
        await pilot.pause()
        assert narrow.panel_open and narrow.query_one("#sidebar").has_class("open")
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert not narrow.query_one("#sidebar").has_class("open")


@pytest.mark.asyncio
async def test_a_turn_fills_the_panel_and_tasks_stay_by_the_input(tmp_path, fake_server):
    from textual.widgets import Input, Static

    from coded.tui import CodedTUI

    with fake_server(_script) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test(size=(140, 40)) as pilot:
            app.agent.todos.items = [{"content": "Write hello.py", "status": "in_progress"}]
            app.query_one("#prompt", Input).value = "write hello"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.pause()

            body = str(app.query_one("#side-body", Static).render())
            assert "Changed last turn" in body and "hello.py" in body and "new +2" in body
            # The tasks stay docked right above the input, not in the panel...
            assert "Write hello.py" not in body
            tasks, prompt = app.query_one("#tasks"), app.query_one("#prompt")
            assert tasks.has_class("shown")
            assert "Write hello.py" in str(tasks.render())
            assert tasks.region.bottom == prompt.region.y
            await pilot.press("ctrl+b")
            await pilot.pause()
            # ...and stay there when the panel is closed.
            assert tasks.has_class("shown") and tasks.region.bottom == prompt.region.y
