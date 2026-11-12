"""Making coded faster: lazy SDK import, disabled tools, compact tool
definitions, and `coded models --speed`."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from coded.agent import create_agent
from coded.config import Config, ModelConfig, load_config
from coded.permissions import PermissionManager
from coded.tools import build_registry
from coded.tools.base import tool_disabled


def _agent(tmp_path, base_url="http://127.0.0.1:9/v1", **model_kw):
    cfg = Config()
    model = ModelConfig(name="m", model="m", base_url=base_url, api_key="k", **model_kw)
    cfg.add_model(model, make_default=True)
    return cfg, create_agent(model=model, config=cfg, cwd=str(tmp_path),
                             permissions=PermissionManager(auto_approve=True),
                             stream=False, verbose=False)


def _names(schema):
    return {t["function"]["name"] for t in schema}


# -- startup --------------------------------------------------------------------

def test_commands_that_need_no_model_do_not_import_the_sdk():
    code = ("import sys, coded.cli, coded.agent; "
            "print('openai' in sys.modules, 'httpx' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.split()
    assert out == ["False", "False"]


def test_the_lazy_sdk_still_works_and_errors_are_still_recognised():
    from coded import llm

    assert llm.openai.APIConnectionError.__name__ == "APIConnectionError"
    client = llm.build_openai_client(ModelConfig(name="x", model="x", base_url="http://h/v1",
                                                 api_key="k"))
    assert client.base_url.host == "h"


# -- disabled tools -------------------------------------------------------------

def test_disabled_tools_are_not_offered_by_name_or_pattern(tmp_path):
    cfg, agent = _agent(tmp_path)
    everything = _names(agent._tool_schema())
    cfg.disabled_tools = ["github", "web_*"]
    left = _names(agent._tool_schema())
    assert {"github", "web_search", "web_fetch"} <= everything
    assert not left & {"github", "web_search", "web_fetch"}
    assert "read" in left and len(left) == len(everything) - 3


def test_a_model_can_disable_more_tools_than_the_rest(tmp_path):
    cfg, agent = _agent(tmp_path, disabled_tools=["task", "memory"])
    cfg.disabled_tools = ["github"]
    assert not _names(agent._tool_schema()) & {"task", "memory", "github"}
    assert cfg.disabled_tools_for(None) == ["github"]


def test_a_call_to_a_disabled_tool_is_refused_with_a_reason(tmp_path, fake_server):
    calls = {"n": 0}

    def script(body):
        calls["n"] += 1
        usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
        if calls["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "echo ZQXJ-ran"})}}]}, usage
        return {"role": "assistant", "content": "ok"}, usage

    with fake_server(script) as server:
        cfg, agent = _agent(tmp_path, base_url=server.base_url)
        cfg.disabled_tools = ["bash"]
        agent.run("run something")
    result = [m for m in agent.session.messages if m.get("role") == "tool"][0]["content"]
    assert "disabled" in result and "ZQXJ-ran" not in result


def test_disabled_tools_load_from_config_files(tmp_path, monkeypatch):
    user = tmp_path / "user.json"
    user.write_text(json.dumps({"disabled_tools": ["github"],
                                "models": {"m": {"model": "m", "disabled_tools": ["task"]}}}))
    cfg = load_config(config_path=str(user))
    assert cfg.disabled_tools == ["github"] and cfg.models["m"].disabled_tools == ["task"]
    assert tool_disabled("mcp__slack__post", ["mcp__slack__*"])
    assert not tool_disabled("read", ["re"])


def test_nothing_changes_without_the_setting(tmp_path):
    # A vision model gets every tool; one that cannot see loses only view_image.
    _, agent = _agent(tmp_path, supports_vision=True)
    assert agent._tool_schema() == agent.registry.openai_schema()
    _, blind = _agent(tmp_path)
    assert blind._tool_schema() == blind.registry.openai_schema(exclude=["view_image"])


# -- compact tool definitions -----------------------------------------------------

def test_the_compact_profile_sends_shorter_tool_definitions(tmp_path):
    _, standard = _agent(tmp_path)
    _, small = _agent(tmp_path, prompt_profile="compact")
    full, compact = json.dumps(standard._tool_schema()), json.dumps(small._tool_schema())
    assert len(compact) < len(full) * 0.85
    assert _names(standard._tool_schema()) == _names(small._tool_schema())


def test_briefs_keep_the_rules_that_prevent_mistakes():
    reg = build_registry()
    brief = {t.name: t.to_openai(compact=True)["function"]["description"] for t in reg.all()}
    assert "interactive" in brief["bash"] and "run_in_background" in brief["bash"]
    assert "cannot ask" in brief["task"]
    assert "approves" in brief["memory"]
    for tool in reg.all():
        assert len(brief[tool.name]) <= len(tool.description.strip())
        # Long argument descriptions are cut, but every argument is still there.
        compact = tool.to_openai(compact=True)["function"]["parameters"]
        assert set(compact.get("properties", {})) == set(tool.parameters.get("properties", {}))
        assert compact.get("required") == tool.parameters.get("required")


def test_the_standard_profile_is_byte_identical():
    reg = build_registry()
    for tool in reg.all():
        assert tool.to_openai() == {"type": "function", "function": {
            "name": tool.name, "description": tool.description.strip(),
            "parameters": tool.parameters}}


# -- coded models --speed ----------------------------------------------------------

def test_speed_measures_first_token_total_and_rate(tmp_path, fake_server):
    from coded.speed import measure, request_for

    def script(body):
        time.sleep(0.05)
        return ({"role": "assistant", "content": "A hash map stores key/value pairs. " * 5},
                {"prompt_tokens": 4321, "completion_tokens": 50, "total_tokens": 4371})

    with fake_server(script) as server:
        cfg, _ = _agent(tmp_path, base_url=server.base_url)
        model = cfg.models["m"]
        messages, tools = request_for(model, cfg, str(tmp_path))
        assert messages[0]["role"] == "system" and tools          # the real turn shape
        result = measure(model, messages, tools, runs=2)
    assert result.ok and not result.estimated
    assert result.first_token >= 0.05 and result.total >= result.first_token
    assert result.prompt_tokens == 4321 and result.output_tokens == 50


def test_minimal_speed_sends_only_the_question(tmp_path):
    from coded.speed import request_for

    cfg, _ = _agent(tmp_path)
    messages, tools = request_for(cfg.models["m"], cfg, str(tmp_path), minimal=True)
    assert tools is None and [m["role"] for m in messages] == ["user"]


def test_a_failing_model_is_reported_and_the_rest_still_measured(tmp_path, capsys, monkeypatch,
                                                                 fake_server):
    from coded import cli

    def script(body):
        return {"role": "assistant", "content": "fine"}, {"prompt_tokens": 9,
                                                         "completion_tokens": 1, "total_tokens": 10}

    with fake_server(script) as server:
        conf = tmp_path / "c.json"
        conf.write_text(json.dumps({"models": {
            "good": {"model": "g", "base_url": server.base_url, "api_key": "k"},
            "down": {"model": "d", "base_url": "http://127.0.0.1:9/v1", "api_key": "k"}},
            "default_model": "good"}))
        code = cli.main(["models", "--config", str(conf), "--cwd", str(tmp_path),
                         "--speed", "--minimal"])
    out = capsys.readouterr().out
    assert code == 1 and "good" in out and "failed" in out


def test_advice_names_the_quickest_and_fastest():
    from coded.speed import SpeedResult, advice

    lines = advice([SpeedResult("a", True, first_token=0.4, total=3, tokens_per_second=40,
                                prompt_tokens=6000),
                    SpeedResult("b", True, first_token=6.0, total=7, tokens_per_second=200,
                                prompt_tokens=6000)])
    text = " ".join(lines)
    assert "Quickest to start answering: a" in text and "Fastest once it starts: b" in text
    assert "disabled_tools" in text
