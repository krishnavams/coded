"""End-to-end tests: drive the agent loop against a fake OpenAI-compatible server."""

from __future__ import annotations

import json

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager


def _make_agent(tmp_path, base_url, stream=False, auto_approve=True):
    cfg = Config()
    model = ModelConfig(name="fake", model="fake", provider="openai-compatible",
                        base_url=base_url, api_key="test")
    cfg.add_model(model, make_default=True)
    perms = PermissionManager(auto_approve=auto_approve)
    return create_agent(model=model, config=cfg, cwd=str(tmp_path),
                        permissions=perms, stream=stream, verbose=False)


def _script_write_then_done(target_name, content):
    """First call → tool_call to write a file; after tool result → final text."""
    def script(body):
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
        if has_tool_result:
            return {"role": "assistant", "content": f"Done, created {target_name}."}, usage
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "write",
                             "arguments": json.dumps({"path": target_name, "content": content})},
            }],
        }, usage
    return script


def test_agent_runs_tool_and_writes_file(tmp_path, fake_server):
    script = _script_write_then_done("agent_created.txt", "hello from agent")
    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url, stream=False)
        result = agent.run("please create the file")
    created = tmp_path / "agent_created.txt"
    assert created.exists()
    assert created.read_text() == "hello from agent"
    assert "Done" in result
    assert agent.session.total_usage.prompt_tokens == 20  # two model calls


def test_agent_streaming_path(tmp_path, fake_server):
    script = _script_write_then_done("streamed.txt", "streamed content")
    with fake_server(script) as server:
        # verbose=False disables the rich Live UI but still exercises streaming parse.
        agent = _make_agent(tmp_path, server.base_url, stream=True)
        agent.verbose = False
        result = agent.run("create it")
    assert (tmp_path / "streamed.txt").read_text() == "streamed content"
    assert "Done" in result


def test_permission_denied_blocks_write(tmp_path, fake_server):
    script = _script_write_then_done("blocked.txt", "should not exist")
    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url, stream=False, auto_approve=False)
        # No prompt callback + not auto-approved => deny by default.
        agent.run("try to create it")
    assert not (tmp_path / "blocked.txt").exists()
