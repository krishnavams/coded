"""Headless tests for the Textual TUI using the Pilot harness."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.tui import CodedTUI, DiffView, ToolCall, ToolResult, UserMessage


def _agent(tmp_path, base_url, auto_approve=True):
    cfg = Config()
    model = ModelConfig(name="fake", model="fake", provider="openai-compatible",
                        base_url=base_url, api_key="k")
    cfg.add_model(model, make_default=True)
    return create_agent(model=model, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(auto_approve=auto_approve),
                        stream=False, verbose=False)


def _write_then_done_script(name, content):
    def script(body):
        usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
        has_tool = any(m.get("role") == "tool" for m in body.get("messages", []))
        if has_tool:
            return {"role": "assistant", "content": "All done!"}, usage
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "write",
                                             "arguments": json.dumps({"path": name, "content": content})}}]}, usage
    return script


@pytest.mark.asyncio
async def test_tui_runs_turn_and_renders(tmp_path, fake_server):
    script = _write_then_done_script("tui_out.txt", "hi from tui")
    with fake_server(script) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test() as pilot:
            from textual.widgets import Input
            app.query_one("#prompt", Input).value = "make a file"
            await pilot.press("enter")
            # Wait for the worker + events to settle.
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.pause()

            # The tool actually ran through the agent loop.
            assert (tmp_path / "tui_out.txt").read_text() == "hi from tui"
            # Transcript contains our message, a tool call, its result, and a diff.
            kinds = {type(w) for w in app.query_one("#transcript").children}
            assert UserMessage in kinds
            assert ToolCall in kinds
            assert ToolResult in kinds
            assert DiffView in kinds


@pytest.mark.asyncio
async def test_tui_permission_modal_allows(tmp_path, fake_server):
    """A write with permission required pops the modal; pressing 'y' lets it run."""
    from coded.tui import PermissionModal

    script = _write_then_done_script("guarded.txt", "approved")
    with fake_server(script) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url, auto_approve=False))
        async with app.run_test() as pilot:
            from textual.widgets import Input
            app.query_one("#prompt", Input).value = "write it"
            await pilot.press("enter")
            # Wait for the worker to reach the permission gate and open the modal.
            for _ in range(80):
                if isinstance(app.screen, PermissionModal):
                    break
                await pilot.pause()
            assert isinstance(app.screen, PermissionModal)
            await pilot.press("y")  # approve
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert (tmp_path / "guarded.txt").read_text() == "approved"


@pytest.mark.asyncio
async def test_tui_clear_action(tmp_path, fake_server):
    def script(body):
        return {"role": "assistant", "content": "hello"}, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    with fake_server(script) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test() as pilot:
            from textual.widgets import Input
            app.query_one("#prompt", Input).value = "hi"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_clear()
            await pilot.pause()
            # After clear, only the "cleared" status note remains.
            from coded.tui import StatusNote
            children = app.query_one("#transcript").children
            assert all(isinstance(w, StatusNote) for w in children)
