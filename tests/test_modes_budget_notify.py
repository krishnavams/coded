"""Accept-edits mode, /permissions, the spending limit and notifications."""

from __future__ import annotations

import io
import json

import pytest

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.notify import Notifier, desktop_command, send
from coded.permissions import Decision, PermissionManager


def _agent(tmp_path, base_url="http://x/v1", **cfg_kw):
    cfg = Config(**cfg_kw)
    m = ModelConfig(name="m", model="m", base_url=base_url, api_key="k",
                    input_cost=1000.0, output_cost=1000.0)
    cfg.add_model(m, make_default=True)
    return create_agent(model=m, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(), stream=False, verbose=False)


# -- accept edits ----------------------------------------------------------------

def test_accept_edits_lets_file_edits_through_but_not_commands():
    asked = []
    perms = PermissionManager(prompt=lambda t, d: asked.append(t) or "no",
                              rules={"deny": ["edit(secret*)"]})
    perms.accept_edits = True
    assert perms.request(key="edit", title="Edit", detail="a.py") is Decision.ALLOW
    assert perms.request(key="write", title="Write", detail="b.py") is Decision.ALLOW
    assert perms.request(key="bash", title="Run", detail="ls") is Decision.DENY
    assert perms.request(key="delete", title="Delete", detail="c.py") is Decision.DENY
    assert asked == ["Run", "Delete"]  # those still asked
    assert perms.request(key="edit", title="Edit", detail="secret.txt") is Decision.DENY
    perms.tainted, perms.injection_guard = True, "strict"
    assert perms.request(key="edit", title="Edit", detail="a.py") is Decision.DENY


def test_shift_tab_cycle_and_config(tmp_path):
    agent = _agent(tmp_path)
    assert agent.permission_mode == "default"
    assert agent.cycle_mode() == "accept-edits" and agent.permissions.accept_edits
    assert agent.cycle_mode() == "plan" and not agent.permissions.accept_edits
    assert agent.permissions.plan_mode
    assert agent.cycle_mode() == "default" and not agent.permissions.plan_mode
    assert _agent(tmp_path, accept_edits=True).permission_mode == "accept-edits"


def test_cli_flags(tmp_path):
    from coded.cli import _apply_overrides, build_parser

    cfg = Config()
    _apply_overrides(cfg, build_parser().parse_args(["--accept-edits", "--max-cost", "1.5"]))
    assert cfg.accept_edits and cfg.max_cost == 1.5


# -- /permissions ------------------------------------------------------------------

def _repl(agent):
    from coded.repl import Repl

    return Repl(agent, agent.config)


def test_permissions_command_edits_rules_mode_and_saves(tmp_path, headless, capsys):
    agent = _agent(tmp_path)
    repl = _repl(agent)
    repl._handle_slash("/permissions allow bash(git *)")
    repl._handle_slash("/permissions deny bash(rm *)")
    assert agent.permissions.allow_rules == ["bash(git *)"]
    assert agent.permissions.deny_rules == ["bash(rm *)"]
    repl._handle_slash("/permissions mode accept-edits")
    assert agent.permission_mode == "accept-edits"
    capsys.readouterr()
    repl._handle_slash("/permissions")
    out = capsys.readouterr().out
    assert "accept-edits" in out and "bash(git *)" in out and "bash(rm *)" in out
    repl._handle_slash("/permissions remove bash(rm *)")
    assert agent.permissions.deny_rules == []

    cfg_file = tmp_path / ".coded" / "config.json"
    cfg_file.parent.mkdir()
    cfg_file.write_text(json.dumps({"max_turns": 7}))
    repl._handle_slash("/permissions save")
    saved = json.loads(cfg_file.read_text())
    assert saved == {"max_turns": 7, "permissions": {"allow": ["bash(git *)"], "deny": []}}


# -- spending limit ----------------------------------------------------------------

def test_the_turn_stops_once_the_budget_is_spent(tmp_path, fake_server):
    calls = []

    def script(body):
        calls.append(1)
        usage = {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000}
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{len(calls)}", "type": "function",
            "function": {"name": "ls", "arguments": "{}"}}]}, usage

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url, max_cost=1.5)  # $2 per call
        answer = agent.run("loop forever")
        assert len(calls) == 1
        assert "max_cost" in answer
        agent.config.max_cost = None
        assert agent.over_budget() is None


def test_budget_command(tmp_path, headless):
    agent = _agent(tmp_path)
    repl = _repl(agent)
    repl._handle_slash("/budget 2.50")
    assert agent.config.max_cost == 2.5
    repl._handle_slash("/budget off")
    assert agent.config.max_cost is None


# -- notifications -----------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_only_long_turns_notify():
    clock, sent = _Clock(), []
    n = Notifier("bell", after=30, clock=clock, sender=lambda *a: sent.append(a) or a[2])
    n.start()
    clock.t = 5
    assert n.finished("quick") is None
    n.start()
    clock.t = 100
    assert n.waiting("Run shell command") == "bell"
    assert n.finished("All done.\nTests pass.") == "bell"
    assert sent[-1] == ("coded finished", "All done. Tests pass.", "bell")
    off = Notifier("off", after=0, clock=clock, sender=lambda *a: sent.append(a))
    off.start()
    assert off.finished() is None


def test_send_rings_the_bell_or_shows_a_desktop_notification():
    out = io.StringIO()
    assert send("t", "m", "bell", out=out) == "bell" and out.getvalue() == "\a"
    spawned = []
    got = send("t", 'say "hi"', "desktop", platform="darwin", which=lambda n: n,
               spawn=lambda argv, **kw: spawned.append(argv))
    assert got == "desktop" and spawned[0][0] == "osascript"
    assert '\\"hi\\"' in spawned[0][2]
    assert desktop_command("t", "m", "linux", lambda n: n)[0] == "notify-send"
    out = io.StringIO()
    assert send("t", "m", "desktop", platform="linux", which=lambda n: None, out=out) == "bell"
    assert send("t", "m", "off") == "off"
