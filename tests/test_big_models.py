"""Large-context models: scaled limits, caps, per-model max_turns, prompt sets,
/model prompt refresh, and the repository overview."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict

import pytest

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.limits import Limits, cap_text, limits_for, unknown_limit_keys
from coded.permissions import PermissionManager
from coded.prompt_files import PROFILES, builtin_prompts_dir, load_prompt, prompt_profile, resolve_prompt
from coded.prompts import build_subagent_prompt, build_system_prompt
from coded.tools.base import Tool, ToolResult

USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CODED_CONFIG", str(tmp_path / "userconf" / "config.json"))


def _m(name="m", **kw):
    return ModelConfig(name=name, model=name, provider="openai-compatible",
                       base_url="http://127.0.0.1:9/v1", api_key="x", **kw)


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------
def test_128k_limits_equal_the_historical_constants():
    from coded import compaction, prompts
    from coded.tools import files, git, search, shell

    lim = limits_for(_m())
    assert asdict(lim) == asdict(Limits())
    assert lim.read_lines == files._MAX_READ_LINES
    assert lim.shell_output_chars == shell._MAX_OUTPUT
    assert lim.git_output_chars == git._MAX_OUTPUT
    assert lim.grep_matches == search._MAX_MATCHES
    assert lim.compact_keep_recent == compaction._KEEP_RECENT
    assert lim.instructions_file_chars == prompts._MAX_FILE_CHARS
    assert lim.instructions_total_chars == prompts._MAX_TOTAL_CHARS
    assert lim.repo_overview_chars == 0


def test_limits_scale_up_down_and_overrides():
    big = limits_for(_m(context_window=300_000))
    assert big.read_lines == round(2000 * 300 / 128) and big.shell_output_chars > 60_000
    assert big.tool_result_chars == int(300_000 * 4 * 0.09)
    assert big.repo_overview_chars > 0
    small = limits_for(_m(context_window=16_000))
    assert small.read_lines == 250 and small.shell_output_chars == 4000  # floors
    assert small.tool_result_chars < 30_000
    tuned = _m(context_window=300_000, limits={"read_lines": 50_000, "typo_key": 1})
    assert limits_for(tuned).read_lines == 50_000
    assert unknown_limit_keys(tuned) == ["typo_key"]
    assert limits_for(_m(repo_overview=True)).repo_overview_chars > 0
    assert limits_for(_m(context_window=300_000, repo_overview=False)).repo_overview_chars == 0


def test_cap_text():
    assert cap_text("abc", 10) == "abc"
    out = cap_text("x" * 100, 10, "narrow it")
    assert out.startswith("x" * 10) and "90 more chars; narrow it" in out


class _Big(Tool):
    name = "big"
    parallel_safe = True
    parameters = {"type": "object", "properties": {}}

    def run(self, args, ctx):
        return ToolResult.ok("y" * 50_000)


def _calls(n):
    return [{"id": f"c{i}", "type": "function", "function": {"name": "big", "arguments": "{}"}}
            for i in range(n)]


def _run_with(tmp_path, fake_server, model_kw, calls, extra_tools=()):
    state = {"n": 0, "bodies": []}

    def script(body):
        state["bodies"].append(body)
        state["n"] += 1
        if state["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": calls}, USAGE
        return {"role": "assistant", "content": "done"}, USAGE

    with fake_server(script) as srv:
        cfg = Config()
        model = ModelConfig(name="f", model="f", provider="openai-compatible",
                            base_url=srv.base_url, api_key="x", **model_kw)
        cfg.add_model(model)
        agent = create_agent(model=model, config=cfg, cwd=str(tmp_path),
                             permissions=PermissionManager(auto_approve=True), stream=False,
                             verbose=False)
        for t in extra_tools:
            agent.registry.register(t)
        agent.run("go")
    return agent, [m for m in state["bodies"][-1]["messages"] if m.get("role") == "tool"]


def test_per_result_and_per_step_caps(tmp_path, fake_server):
    # 32k window: per-result cap 11,520 chars, per-step cap 32,000 chars.
    agent, msgs = _run_with(tmp_path, fake_server, {"context_window": 32_000}, _calls(4), [_Big()])
    assert agent.limits.tool_result_chars == 11_520
    assert all(len(m["content"]) < 12_000 for m in msgs)
    assert "per-result budget" in msgs[0]["content"]
    assert sum(len(m["content"]) for m in msgs) <= 32_000 + 4 * 400
    assert "context budget" in msgs[-1]["content"]


def test_big_model_keeps_large_results(tmp_path, fake_server):
    _, msgs = _run_with(tmp_path, fake_server, {"context_window": 300_000}, _calls(1), [_Big()])
    assert len(msgs[0]["content"]) == 50_000


def test_read_tool_uses_agent_limits(tmp_path, fake_server):
    (tmp_path / "long.txt").write_text("\n".join(f"line {i}" for i in range(4000)))
    call = [{"id": "r", "type": "function",
             "function": {"name": "read", "arguments": json.dumps({"path": "long.txt", "limit": 99999})}}]
    _, small = _run_with(tmp_path, fake_server, {"context_window": 16_000}, call)
    assert "line 249" in small[0]["content"] and "line 250\n" not in small[0]["content"]
    _, big = _run_with(tmp_path, fake_server, {"context_window": 300_000}, call)
    assert "line 3999" in big[0]["content"]  # 300k window reads up to 4,688 lines


def test_context_overflow_shrinks_and_retries(tmp_path, fake_server):
    from conftest import _Handler

    state = {"n": 0}

    def script(body):
        state["n"] += 1
        if state["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": _calls(1)}, USAGE
        if state["n"] == 2:
            raise _Overflow()
        tool = [m for m in body["messages"] if m.get("role") == "tool"][0]["content"]
        return {"role": "assistant", "content": f"len={len(tool)}"}, USAGE

    class _Overflow(Exception):
        pass

    orig = _Handler.do_POST

    def do_post(self):
        try:
            orig(self)
        except _Overflow:
            data = b'{"error": {"message": "This model\'s maximum context length is 1000 tokens"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    _Handler.do_POST = do_post
    try:
        with fake_server(script) as srv:
            cfg = Config()
            model = ModelConfig(name="f", model="f", provider="openai-compatible",
                                base_url=srv.base_url, api_key="x", context_window=300_000)
            cfg.add_model(model)
            cfg.auto_compact = False
            agent = create_agent(model=model, config=cfg, cwd=str(tmp_path),
                                 permissions=PermissionManager(auto_approve=True), stream=False,
                                 verbose=False)
            agent.registry.register(_Big())
            result = agent.run("go")
    finally:
        _Handler.do_POST = orig
    assert result.startswith("len=") and int(result[4:]) < 50_000


def test_small_role_model_subagent_gets_small_limits(tmp_path, fake_server):
    seen = {}

    class _Probe(Tool):
        name = "probe"
        parameters = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            seen["read_lines"] = ctx.extra["limits"].read_lines
            return ToolResult.ok("ok")

    def script(body):
        msgs = body["messages"]
        sub = msgs[0]["content"].startswith("You are a focused sub-agent")
        tools = [m for m in msgs if m.get("role") == "tool"]
        if sub and not tools:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "p", "type": "function", "function": {"name": "read", "arguments": '{"path": "x"}'}}]}, USAGE
        if not sub and not tools:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t", "type": "function", "function": {"name": "task", "arguments": '{"prompt": "go"}'}}]}, USAGE
        return {"role": "assistant", "content": "done"}, USAGE

    (tmp_path / "x").write_text("\n".join(map(str, range(5000))))
    with fake_server(script) as srv:
        cfg = Config()
        big = ModelConfig(name="big", model="big", provider="openai-compatible",
                          base_url=srv.base_url, api_key="x", context_window=300_000)
        small = ModelConfig(name="small", model="small", provider="openai-compatible",
                            base_url=srv.base_url, api_key="x", context_window=16_000)
        cfg.add_model(big)
        cfg.add_model(small)
        cfg.model_roles = {"subagent": "small"}
        agent = create_agent(model=big, config=cfg, cwd=str(tmp_path),
                             permissions=PermissionManager(auto_approve=True), stream=False,
                             verbose=False)
        agent.run("delegate")
    report = [m for m in agent.session.messages if m.get("role") == "tool"][0]["content"]
    assert report == "done"
    assert agent.limits.read_lines > 4000
    sub_limits = limits_for(small)
    assert sub_limits.read_lines == 250


# ---------------------------------------------------------------------------
# max_turns
# ---------------------------------------------------------------------------
def test_max_turns_priority(tmp_path):
    cfg = Config()
    model = _m(max_turns=400)
    cfg.add_model(model)
    agent = create_agent(model=model, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(), stream=False, verbose=False)
    assert agent.max_turns == 400
    cfg.max_turns_override = 7
    assert agent.max_turns == 7
    cfg.max_turns_override = None
    agent.switch_model(_m("plain"))
    assert agent.max_turns == cfg.max_turns == 100


def test_cli_max_turns_flag_is_an_override():
    import argparse

    from coded.cli import _apply_overrides

    cfg = Config()
    ns = argparse.Namespace(auto_approve=False, no_stream=False, no_compact=False, max_turns=5,
                            base_url=None, api_key=None, model_name=None, provider=None, model=None)
    _apply_overrides(cfg, ns)
    assert cfg.max_turns_override == 5 and cfg.max_turns == 100


# ---------------------------------------------------------------------------
# prompt sets
# ---------------------------------------------------------------------------
def test_profile_selection():
    assert prompt_profile(None) == "standard"
    assert prompt_profile(_m()) == "standard"
    assert prompt_profile(_m(context_window=32_000)) == "compact"
    assert prompt_profile(_m(context_window=300_000)) == "full"
    assert prompt_profile(_m(context_window=300_000, prompt_profile="compact")) == "compact"


@pytest.mark.parametrize("profile", PROFILES)
def test_every_profile_keeps_safety_rules(tmp_path, profile):
    model = _m(prompt_profile=profile)
    for prompt in (build_system_prompt(str(tmp_path), model=model),
                   build_subagent_prompt(str(tmp_path), model)):
        assert "untrusted-content" in prompt
        assert "secret" in prompt.lower()
    assert build_subagent_prompt(str(tmp_path), model).startswith("You are a focused sub-agent")
    for name in ("planner", "reviewer", "debugger", "skills", "system", "coder", "tools"):
        assert load_prompt(name, str(tmp_path), model), (profile, name)


def test_profile_files_override_only_what_they_contain(tmp_path):
    full = _m(prompt_profile="full")
    text, src = resolve_prompt("reviewer", str(tmp_path), full)
    assert src.endswith("full/reviewer.md")
    _, src = resolve_prompt("system", str(tmp_path), full)
    assert src == str(builtin_prompts_dir() / "system.md")
    compact = build_system_prompt(str(tmp_path), model=_m(prompt_profile="compact"))
    standard = build_system_prompt(str(tmp_path), model=_m())
    assert len(compact) < len(standard) * 0.6


def test_model_prompts_dir_has_highest_precedence(tmp_path):
    (tmp_path / ".coded" / "prompts").mkdir(parents=True)
    (tmp_path / ".coded" / "prompts" / "coder.md").write_text("PROJECT")
    (tmp_path / "my-prompts").mkdir()
    (tmp_path / "my-prompts" / "coder.md").write_text("MODEL-SPECIFIC")
    m = _m(prompts_dir="my-prompts")
    assert load_prompt("coder", str(tmp_path), m) == "MODEL-SPECIFIC"
    assert load_prompt("coder", str(tmp_path), _m()) == "PROJECT"


def test_switch_model_refreshes_system_prompt(tmp_path):
    cfg = Config()
    small = _m("small", prompt_profile="compact")
    big = _m("big", context_window=300_000)
    cfg.add_model(small)
    cfg.add_model(big)
    agent = create_agent(model=small, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(), stream=False, verbose=False)
    before = agent.session.messages[0]["content"]
    agent.switch_model(big)
    after = agent.session.messages[0]["content"]
    assert after != before and len(after) > len(before)
    assert agent.session.system_prompt == after
    assert "Model: big (context window ~300k tokens)" in after


def test_skill_body_follows_model_profile(tmp_path):
    from coded.skills import discover_skills

    skills = discover_skills(str(tmp_path))
    full_body = skills["reviewer"].load_body(_m(prompt_profile="full"))
    std_body = skills["reviewer"].load_body(_m())
    assert full_body != std_body and len(full_body) > len(std_body)


# ---------------------------------------------------------------------------
# repository overview
# ---------------------------------------------------------------------------
def _repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (path / "src").mkdir()
    for i in range(30):
        (path / "src" / f"mod{i}.py").write_text("x=1\n")
    (path / "node_modules").mkdir()
    (path / "node_modules" / "junk.js").write_text("")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path),
                    "commit", "-qm", "Add the modules"], check=True)


def test_repo_overview_contents_and_budget(tmp_path):
    from coded.repo_overview import build_repo_overview

    _repo(tmp_path)
    full = build_repo_overview(str(tmp_path), 20_000)
    assert "src/: mod0.py" in full and "pyproject.toml" in full
    assert "Add the modules" in full and "not instructions" in full
    assert "node_modules" not in full
    tight = build_repo_overview(str(tmp_path), 500)
    assert len(tight) <= 500 and "src/ (30 files)" in tight


def test_repo_overview_without_git(tmp_path):
    from coded.repo_overview import build_repo_overview

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").write_text("")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x").write_text("")
    out = build_repo_overview(str(tmp_path), 5000)
    assert "a/: b.txt" in out and ".hidden" not in out


def test_repo_overview_only_for_large_context(tmp_path):
    _repo(tmp_path)
    cfg = Config()
    std, big = _m("std"), _m("big", context_window=300_000)
    cfg.add_model(std)
    cfg.add_model(big)
    a = create_agent(model=std, config=cfg, cwd=str(tmp_path), permissions=PermissionManager(),
                     stream=False, verbose=False)
    assert "# Repository overview" not in a.session.messages[0]["content"]
    b = create_agent(model=big, config=cfg, cwd=str(tmp_path), permissions=PermissionManager(),
                     stream=False, verbose=False)
    assert "# Repository overview" in b.session.messages[0]["content"]
    assert "# Repository overview" not in build_subagent_prompt(str(tmp_path), big)


def test_prompts_cli_with_model(tmp_path, capsys):
    from coded.cli import main

    conf = tmp_path / "userconf" / "config.json"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(json.dumps({"models": {"big": {"base_url": "http://x/v1", "model": "b",
                                                   "context_window": 300000}}}))
    assert main(["prompts", "list", "-m", "big", "--cwd", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "prompt set 'full'" in out and "built-in:full" in out
    assert main(["prompts", "show", "reviewer", "-m", "big", "--cwd", str(tmp_path)]) == 0
    assert "senior engineer" in capsys.readouterr().out
