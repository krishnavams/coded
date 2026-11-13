"""Command Center: /dashboard shows agents, models, tool usage, checks and problems."""

from __future__ import annotations

import io
import json

import pytest

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.dashboard import render
from coded.permissions import PermissionManager

_U = {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}


def _plain(renderable) -> str:
    from rich.console import Console

    con = Console(width=120, record=True, file=io.StringIO(), color_system=None)
    con.print(renderable)
    return con.export_text()


def _call(name, args, cid):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


def _ran_agent(tmp_path, fake_server):
    (tmp_path / "a.txt").write_text("hi")
    steps = [_call("read", {"path": "a.txt"}, "c1"), _call("read", {"path": "gone.txt"}, "c2"),
             _call("bash", {"command": "rm -rf build"}, "c3")]

    def script(body):
        n = sum(1 for m in body["messages"] if m["role"] == "tool")
        return (steps[n] if n < len(steps) else {"role": "assistant", "content": "ok"}), _U

    with fake_server(script) as server:
        cfg = Config()
        m = ModelConfig(name="fake", model="fake", base_url=server.base_url, api_key="k",
                        input_cost=1.0, output_cost=2.0)
        cfg.add_model(m, make_default=True)
        perms = PermissionManager()
        perms.set_prompt(lambda t, d: "no")
        agent = create_agent(model=m, config=cfg, cwd=str(tmp_path), permissions=perms,
                             stream=False, verbose=False)
        agent.run("clean the build folder")
    return agent


def test_dashboard_shows_the_session(tmp_path, fake_server):
    agent = _ran_agent(tmp_path, fake_server)
    agent.tasks.finish(agent.tasks.start("search the api", "fake"), "found", "completed")
    text = _plain(render(agent))
    for want in ("Command Center", "fake", "1 request (4 model calls)", "3 tool calls", "2 failed or denied",
                 "Agents", "main", "search the api", "completed",
                 "Models", "Tools", "read", "bash", "Recent problems", "gone.txt", "denied by you",
                 "checks off"):
        assert want in text, want
    # Per-model cost comes from the llm events: 4 calls x (50 in, 20 out).
    assert "200" in text and "$0.0004" in text


def test_repl_dashboard_and_mid_turn(tmp_path, fake_server, headless, monkeypatch):
    from rich.console import Console

    from coded import ui
    from coded.repl import Repl

    agent = _ran_agent(tmp_path, fake_server)
    repl = Repl(agent, agent.config)
    con = Console(width=120, record=True, file=io.StringIO(), color_system=None)
    monkeypatch.setattr(ui, "console", con)
    repl._handle_slash("/dashboard")
    assert "Command Center" in con.export_text()
    assert repl._intercept_midturn("/dashboard") is True  # shown at once, not queued
    assert "working" in con.export_text()
    assert repl._intercept_midturn("fix the tests") is False


@pytest.mark.asyncio
async def test_tui_dashboard_modal(tmp_path, fake_server):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from coded.tui import CodedTUI, DashboardModal

    agent = _ran_agent(tmp_path, fake_server)
    app = CodedTUI(agent)
    async with app.run_test() as pilot:
        app.query_one("#prompt", Input).value = "/dashboard"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DashboardModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DashboardModal)


def test_dashboard_with_a_task_list(tmp_path, fake_server):
    agent = _ran_agent(tmp_path, fake_server)
    agent.todos.items = [{"content": "scan the tests", "status": "in_progress"},
                         {"content": "fix them", "status": "pending"}]
    text = _plain(render(agent))
    assert "0/2" in text and "now: scan the tests" in text
