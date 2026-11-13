"""max_turns grows while a request is making progress and stops when it loops."""

from __future__ import annotations

import json

from coded.agent import create_agent, stuck_reason
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager

_U = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def _call(path, cid):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": "read", "arguments": json.dumps({"path": path})}}]}


def _agent(tmp_path, server, **cfg_kw):
    cfg = Config(**cfg_kw)
    m = ModelConfig(name="fake", model="fake", base_url=server.base_url, api_key="k")
    cfg.add_model(m, make_default=True)
    perms = PermissionManager()
    perms.set_prompt(lambda t, d: "yes")
    return create_agent(model=m, config=cfg, cwd=str(tmp_path), permissions=perms,
                        stream=False, verbose=False)


def _run(tmp_path, fake_server, calls, **cfg_kw):
    """Run one request whose model makes `calls` tool calls, then answers."""
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(str(i))
    n = {"i": 0}

    def script(body):
        n["i"] += 1
        if n["i"] <= len(calls):
            return _call(calls[n["i"] - 1], f"c{n['i']}"), _U
        return {"role": "assistant", "content": "done"}, _U

    with fake_server(script) as server:
        agent = _agent(tmp_path, server, **cfg_kw)
        answer = agent.run("go")
    kinds = [e["kind"] for e in agent.events.items]
    return agent, answer, kinds


def test_progress_extends_the_limit(tmp_path, fake_server):
    # 12 different calls with max_turns 5: extended twice (10, 15), then it answers.
    calls = [f"f{i % 5}.txt" if i < 5 else f"missing{i}.txt" for i in range(12)]
    agent, answer, kinds = _run(tmp_path, fake_server, calls, max_turns=5)
    assert answer == "done"
    assert kinds.count("turns_extended") == 2 and "max_turns" not in kinds


def test_a_loop_is_stopped_at_the_limit(tmp_path, fake_server):
    agent, answer, kinds = _run(tmp_path, fake_server, ["f0.txt"] * 20, max_turns=5)
    assert answer != "done" and "max_turns" in kinds and "turns_extended" not in kinds


def test_the_ceiling_still_holds(tmp_path, fake_server):
    # Distinct paths, alternating good and missing files: progress, never a loop.
    calls = [f"f{i // 2 % 5}.txt" if i % 2 == 0 else f"nope{i}.txt" for i in range(40)]
    agent, answer, kinds = _run(tmp_path, fake_server, calls, max_turns=2)
    # Ceiling: 5 x max_turns = 10 steps.
    assert answer != "done" and "max_turns" in kinds
    assert agent.session.turns == 10


def test_off_or_explicit_cli_limit_is_a_hard_cap(tmp_path, fake_server):
    calls = [f"nope{i}.txt" for i in range(9)]
    _, answer, kinds = _run(tmp_path, fake_server, calls, max_turns=3, auto_extend_turns=False)
    assert answer != "done" and "turns_extended" not in kinds
    _, answer, kinds = _run(tmp_path, fake_server, calls, max_turns=50, max_turns_override=3)
    assert answer != "done" and "turns_extended" not in kinds


def test_stuck_reason():
    assert stuck_reason([("read", '{"path": "a"}', False)] * 3) is None
    assert "same `read` call 4 times" in stuck_reason([("read", '{"path": "a"}', False)] * 4)
    fails = [("bash", f'{{"command": "x{i}"}}', True) for i in range(10)]
    assert "all failed" in stuck_reason(fails)
    assert stuck_reason(fails[:9] + [("bash", "{}", False)]) is None
