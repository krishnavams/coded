"""/rewind, /context, /export, cached-token pricing and stream-json output."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coded.agent import create_agent
from coded.checkpoints import CheckpointManager
from coded.config import Config, ModelConfig
from coded.llm import Usage, usage_from
from coded.permissions import PermissionManager
from coded.session import usage_cost


def _agent(tmp_path, base_url, **model_kw):
    cfg = Config()
    m = ModelConfig(name="fake", model="fake", base_url=base_url, api_key="k", **model_kw)
    cfg.add_model(m, make_default=True)
    return create_agent(model=m, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(auto_approve=True), stream=False,
                        verbose=False, checkpoints=CheckpointManager(str(tmp_path)))


def _writer(body):
    """Each request writes the file it names, then answers."""
    msgs = body["messages"]
    if msgs[-1]["role"] == "tool":
        return {"role": "assistant", "content": "done"}, None
    name = msgs[-1]["content"].split()[-1]
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": f"c-{name}", "type": "function",
        "function": {"name": "write", "arguments": json.dumps({"path": name, "content": name})},
    }]}, None


# -- /rewind ----------------------------------------------------------------------

def test_rewind_drops_later_requests_and_their_file_changes(tmp_path, fake_server):
    with fake_server(_writer) as server:
        agent = _agent(tmp_path, server.base_url)
        agent.run("write one.txt")
        agent.run("write two.txt")
        agent.run("write three.txt")
    assert [t["text"] for t in agent.turn_history()] == [
        "write one.txt", "write two.txt", "write three.txt"]

    summary, text = agent.rewind(2)
    assert text == "write two.txt"
    assert "Rewound 2 request(s)" in summary
    assert (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists() and not (tmp_path / "three.txt").exists()
    users = [m["content"] for m in agent.session.messages if m["role"] == "user"]
    assert users == ["write one.txt"]
    assert len(agent.turn_history()) == 1


def test_rewind_can_keep_files_and_checks_the_number(tmp_path, fake_server):
    import pytest

    with fake_server(_writer) as server:
        agent = _agent(tmp_path, server.base_url)
        agent.run("write one.txt")
    with pytest.raises(ValueError):
        agent.rewind(5)
    summary, _ = agent.rewind(1, restore_files=False)
    assert "left as they are" in summary and (tmp_path / "one.txt").exists()
    assert [m["role"] for m in agent.session.messages] == ["system"]


def test_rewind_command_prefills_the_request(tmp_path, fake_server, headless):
    from coded.repl import Repl

    with fake_server(_writer) as server:
        agent = _agent(tmp_path, server.base_url)
        agent.run("write one.txt")
        repl = Repl(agent, agent.config)
        repl._handle_slash("/rewind 1")
    assert repl._prefill == "write one.txt"
    assert not (tmp_path / "one.txt").exists()


# -- cached tokens --------------------------------------------------------------------

def test_cached_tokens_are_read_and_priced():
    raw = SimpleNamespace(prompt_tokens=1000, completion_tokens=10,
                          prompt_tokens_details=SimpleNamespace(cached_tokens=800))
    assert usage_from(raw) == Usage(1000, 10, 800)
    deepseek = SimpleNamespace(prompt_tokens=100, completion_tokens=1, prompt_cache_hit_tokens=60,
                               prompt_tokens_details=None)
    assert usage_from(deepseek).cached_tokens == 60
    m = ModelConfig(name="m", model="m", input_cost=1.0, output_cost=0.0,
                    cached_input_cost=0.1)
    assert abs(usage_cost(Usage(1_000_000, 0, 800_000), m) - (0.2 + 0.08)) < 1e-9
    m.cached_input_cost = None  # not set: cached tokens cost the same
    assert abs(usage_cost(Usage(1_000_000, 0, 800_000), m) - 1.0) < 1e-9


def test_cached_tokens_flow_from_the_endpoint_to_the_session(tmp_path, fake_server):
    usage = {"prompt_tokens": 1000, "completion_tokens": 0, "total_tokens": 1000,
             "prompt_tokens_details": {"cached_tokens": 900}}
    with fake_server(lambda body: ({"role": "assistant", "content": "hi"}, usage)) as server:
        agent = _agent(tmp_path, server.base_url, input_cost=10.0, cached_input_cost=1.0)
        agent.run("hello")
    assert agent.session.total_usage.cached_tokens == 900
    assert abs(agent.session.estimated_cost(agent.model) - (100 * 10 + 900 * 1) / 1e6) < 1e-12


# -- /context and /export -----------------------------------------------------------

def test_context_breakdown_and_export(tmp_path, fake_server):
    from coded.transcript import context_breakdown, export_markdown, render_context, write_export

    with fake_server(_writer) as server:
        agent = _agent(tmp_path, server.base_url)
        agent.run("write one.txt")
    info = context_breakdown(agent)
    names = [n for n, _ in info["parts"]]
    assert names[0] == "System prompt" and names[1].startswith("Tool definitions (")
    assert all(n >= 0 for _, n in info["parts"]) and info["estimated"] > 0
    text = render_context(info)
    assert "window 128,000 tokens" in text and "Tool results" in text

    md = export_markdown(agent)
    assert "## You\n\nwrite one.txt" in md and "Tool call: `write`" in md
    assert "<summary>Result: write</summary>" in md and "## coded\n\ndone" in md
    path = write_export(agent, "out/session.md")
    assert path == tmp_path / "out" / "session.md" and path.read_text() == md


# -- stream-json ------------------------------------------------------------------------

def test_stream_json_prints_one_event_per_line(tmp_path, fake_server, capsys):
    from coded.cli import _run_print_mode

    with fake_server(_writer) as server:
        agent = _agent(tmp_path, server.base_url)
        agent.stream = True
        code = _run_print_mode(agent, "write one.txt", output_format="stream-json")
    assert code == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "result"
    assert "tool_call" in kinds and "tool_result" in kinds
    assert events[-1]["content"] == "done" and "usage" in events[-1]
