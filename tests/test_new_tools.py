"""Tools copied from other agents: ask_user, view_image, enter_plan_mode, MCP resources."""

from __future__ import annotations

import base64
import json
import sys

import pytest

from coded import ui
from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.tools import build_registry
from coded.tools.base import ToolContext
from coded.tools.interact import AskUserTool, ViewImageTool

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
_USAGE = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def _call(name, args, cid="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _agent(tmp_path, base_url, vision=False, perms=None):
    cfg = Config()
    m = ModelConfig(name="fake", model="fake", provider="openai-compatible",
                    base_url=base_url, api_key="k", supports_vision=vision)
    cfg.add_model(m, make_default=True)
    return create_agent(model=m, config=cfg, cwd=str(tmp_path),
                        permissions=perms or PermissionManager(auto_approve=True),
                        stream=False, verbose=False)


def _ctx(tmp_path, perms=None, **extra):
    return ToolContext(cwd=str(tmp_path), permissions=perms or PermissionManager(),
                       config=Config(), extra=extra)


# -- ask_user --------------------------------------------------------------------
def test_ask_user_returns_the_answer_or_says_what_to_do(tmp_path):
    perms = PermissionManager()
    tool = AskUserTool()
    args = {"question": "Which database?", "options": ["Postgres", "SQLite"]}
    # Nobody to ask (--print, ACP): carry on with an assumption.
    assert "reasonable assumption" in tool.run(args, _ctx(tmp_path, perms)).content
    seen = []
    perms.set_question(lambda q, opts: seen.append((q, opts)) or "SQLite")
    assert tool.run(args, _ctx(tmp_path, perms)).content == "The user answered: SQLite"
    assert seen == [("Which database?", ["Postgres", "SQLite"])]
    perms.set_question(lambda q, opts: "")
    assert "skipped" in tool.run(args, _ctx(tmp_path, perms)).content
    assert tool.run({"question": " "}, _ctx(tmp_path, perms)).is_error


def test_pick_answer_maps_numbers_to_choices():
    assert ui.pick_answer("2", ["a", "b"]) == "b"
    assert ui.pick_answer(" 7 ", ["a", "b"]) == "7"
    assert ui.pick_answer("my own", ["a"]) == "my own"
    assert ui.pick_answer(None, ["a"]) is None
    ui.question_panel("Which?", ["a", "b"])  # renders without error


def test_repl_question_prompt(tmp_path, headless, monkeypatch):
    from coded.repl import Repl

    agent = _agent(tmp_path, "http://x/v1")
    repl = Repl(agent, agent.config)
    monkeypatch.setattr(repl.prompt, "prompt", lambda *a, **k: "1")
    assert agent.permissions.question("Pick one?", ["first", "second"]) == "first"


def test_main_agent_only_tools():
    main, sub = build_registry().names(), build_registry(include_task=False).names()
    for name in ("ask_user", "enter_plan_mode"):
        assert name in main and name not in sub
    assert "view_image" in main and "view_image" in sub


# -- enter_plan_mode -------------------------------------------------------------
def test_enter_plan_mode_switches_the_agent(tmp_path, fake_server):
    def script(body):
        if any(m.get("role") == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "Researching first."}, _USAGE
        return _call("enter_plan_mode", {}), _USAGE

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url)
        agent.run("redesign the storage layer")
    assert agent.mode == "plan"


# -- view_image ------------------------------------------------------------------
def test_view_image_attaches_the_picture_after_the_tool_results(tmp_path, fake_server):
    (tmp_path / "shot.png").write_bytes(_PNG)
    requests = []

    def script(body):
        requests.append(body)
        if any(m.get("role") == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "A single pixel."}, _USAGE
        return _call("view_image", {"path": "shot.png"}), _USAGE

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url, vision=True)
        assert agent.run("what is in shot.png?") == "A single pixel."
    assert "view_image" in [t["function"]["name"] for t in requests[0]["tools"]]
    msgs = requests[1]["messages"]
    tool_at = next(i for i, m in enumerate(msgs) if m["role"] == "tool")
    assert "Attached shot.png" in msgs[tool_at]["content"]
    image_msg = msgs[tool_at + 1]
    assert image_msg["role"] == "user"
    assert image_msg["content"][0]["text"] == "[view_image] shot.png"
    assert image_msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_view_image_is_not_offered_to_a_model_that_cannot_see(tmp_path, fake_server):
    requests = []

    def script(body):
        requests.append(body)
        return {"role": "assistant", "content": "ok"}, _USAGE

    with fake_server(script) as server:
        _agent(tmp_path, server.base_url, vision=False).run("hi")
    assert "view_image" not in [t["function"]["name"] for t in requests[0]["tools"]]


def test_view_image_refusals(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "a.png").write_bytes(_PNG)
    seen = []
    vision = ModelConfig(name="v", model="v", supports_vision=True)
    blind = ModelConfig(name="b", model="b")
    tool = ViewImageTool()
    run = lambda path, model=vision: tool.run(  # noqa: E731
        {"path": path}, _ctx(tmp_path, model=model, attach_image=seen.append))
    assert "cannot see images" in run("a.png", blind).content
    assert "No such file" in run("missing.png").content
    assert "not an image" in run("notes.txt").content
    assert not seen
    assert not run("a.png").is_error and seen == [str(tmp_path / "a.png")]


# -- MCP resources ---------------------------------------------------------------
_SERVER = '''
from mcp.server.mcpserver import MCPServer
app = MCPServer("docs")

@app.resource("docs://readme", description="The project readme")
def readme() -> str:
    return "Hello from the readme"

@app.tool()
def ping() -> str:
    """Ping."""
    return "pong"

app.run()
'''


def test_mcp_resources_are_listed_and_read(tmp_path):
    pytest.importorskip("mcp.server.mcpserver")
    from coded.mcp_client import MCPManager

    (tmp_path / "srv.py").write_text(_SERVER)
    manager = MCPManager({"docs": {"command": sys.executable, "args": [str(tmp_path / "srv.py")]}})
    manager.start(timeout=60)
    try:
        assert not manager.errors, manager.errors
        names = [t.name for t in manager.build_tools(manager)]
        assert {"mcp__docs__ping", "list_mcp_resources", "read_mcp_resource"} <= set(names)
        tools = {t.name: t for t in manager.build_tools(manager)}
        listed = tools["list_mcp_resources"].run({}, _ctx(tmp_path)).content
        assert "[docs] docs://readme" in listed and "The project readme" in listed
        got = tools["read_mcp_resource"].run({"server": "docs", "uri": "docs://readme"},
                                             _ctx(tmp_path))
        assert got.content == "Hello from the readme"
        assert tools["read_mcp_resource"].run({"server": "docs"}, _ctx(tmp_path)).is_error
    finally:
        manager.stop()


def test_no_resource_tools_without_resources():
    from coded.mcp_client import MCPManager

    manager = MCPManager({})
    manager.tool_specs = [("s", "t", "", {})]
    assert [t.name for t in manager.build_tools(manager)] == ["mcp__s__t"]
