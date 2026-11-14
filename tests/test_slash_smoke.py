"""Every slash command survives a session that has real state behind it."""

from __future__ import annotations

import io
import json
import os
import traceback

import pytest

from coded import ui
from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.repl import Repl
from coded.skills import discover_skills

_U = {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}

COMMANDS = [
    "/help", "/models", "/verify", "/memory", "/memory add convention: tabs here [/k] [bold]",
    "/memory add tabs here", "/memory list", "/memory search tabs", "/memory search zzz",
    "/memory search", "/memory forget nope", "/memory add", "/memory wat",
    "/heal", "/heal off", "/lab", "/twin", "/twin", "/redteam", "/redteam a.py",
    "/timeline", "/timeline 1", "/timeline 2", "/timeline 99", "/timeline x",
    "/timeline restore", "/dashboard", "/refactor", "/refactor a.py", "/model",
    "/model fake", "/model nope", "/tools", "/image", "/image nope.png", "/image pic.png",
    "/image pic.png describe it", "/image pic.png -- describe it", "/paste", "/checkpoints",
    "/skills", "/skill", "/skill planner", "/skill planner plan a thing", "/skill nope",
    "/planner", "/planner plan a [/x] thing",
    "/mission", "/mission --list", "/mission --bogus-flag",
    "/review", "/review a.py", "/review --adversarial a.py", "/cost", "/rewind", "/rewind x",
    "/rewind 99", "/style", "/style concise", "/style nope", "/style off", "/style default",
    "/add-dir", "/add-dir .", "/add-dir nope", "/add-dir sub", "/context", "/export",
    "/export out.md", "/permissions", "/permissions allow bash(echo [/o] [a-z]*)", "/permissions allow bash(echo *)",
    "/permissions deny bash(rm *)", "/permissions", "/permissions remove bash(echo *)",
    "/permissions remove nope", "/permissions mode accept-edits", "/permissions mode plan",
    "/permissions mode default", "/permissions mode nope", "/permissions save",
    "/permissions wat", "/budget", "/budget 5", "/budget $5", "/budget x", "/budget off",
    "/config", "/route", "/route on", "/route off", "/todos", "/shells", "/shells kill nope",
    "/tasks", "/tasks t1", "/tasks nope[/m]", "/shells kill x[/m]", "/skill no[/m]", "/model no[/m]", "/style no[/m]", "/add-dir no[/m]", "/image no[/m].png", "/memory forget no[/m]", "/no[/m]", "/tasks stop no[/m]", "/tasks stop nope", "/btw what now [/l]?", "/btw",
    "/agents", "/commands", "/hello", "/hello there [/n] you", "/plugins", "/prompts", "/plan",
    "/plan", "/plan think about it", "/plan", "/init", "/vim", "/vim", "/compact",
    "/dashboard", "/timeline", "/timeline restore 2", "/rewind 2 --keep-files",
    "/rewind 1", "/undo", "/undo", "/nope", "/memory clear", "/clear",
    "/dashboard", "/timeline", "/cost", "/context", "/tasks", "/todos", "/checkpoints",
    "/rewind", "/export", "/compact", "/undo", "/resume", "/resume 1", "/resume no[/m]",
]


def _call(name, args, cid):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture(params=["quiet", "verbose", "streaming"])
def repl(request, tmp_path, fake_server, monkeypatch, headless):
    """A REPL whose session holds text full of "[x]" / "[/x]" (rich markup look-alikes)."""
    monkeypatch.chdir(tmp_path)
    con = ui.TranscriptConsole(width=200, record=True, file=io.StringIO(), color_system=None)
    monkeypatch.setattr(ui, "console", con)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    cmds = tmp_path / ".coded" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "hello.md").write_text("---\ndescription: say hi [/j]\nargument-hint: [name]\n---\nSay hi to $ARGUMENTS\n")
    todos = [{"content": "scan [/a] app/[slug]", "status": "completed", "activeForm": "scanning"},
             {"content": "fix [/b]", "status": "in_progress", "activeForm": "fixing [/b]"},
             {"content": "verify", "status": "pending", "activeForm": "verifying"}]
    steps = [_call("read", {"path": "a.py"}, "c1"), _call("read", {"path": "gone[/c].txt"}, "c2"),
             _call("todo_write", {"todos": todos}, "c3"),
             _call("write", {"path": "b.py", "content": "x = 1\n"}, "c4"),
             _call("edit", {"path": "a.py", "old_string": "1", "new_string": "2"}, "c5"),
             _call("bash", {"command": "echo hi"}, "c6"),
             _call("bash", {"command": "sleep 30 # [/d] [bold]", "run_in_background": True}, "c7")]
    done = {"n": 0}

    def script(body):
        if done["n"] < len(steps):
            done["n"] += 1
            return steps[done["n"] - 1], _U
        return {"role": "assistant", "content": "ok"}, _U

    with fake_server(script) as server:
        cfg = Config()
        m = ModelConfig(name="fake", model="fake", base_url=server.base_url, api_key="k",
                        input_cost=1.0, output_cost=2.0)
        cfg.add_model(m, make_default=True)
        perms = PermissionManager()
        perms.set_prompt(lambda t, d: "yes")
        agent = create_agent(model=m, config=cfg, cwd=str(tmp_path), permissions=perms,
                             skills=discover_skills(str(tmp_path)), stream=request.param == "streaming",
                             verbose=request.param != "quiet")
        agent.run("change a.py [/e] and app/[slug]/page.py")
        agent.run("and again [/f]")
        agent.tasks.finish(agent.tasks.start("search the api [/g]", "fake"), "found [/h]", "completed")
        agent.tasks.start("still going [/i]", "fake")
        r = Repl(agent, cfg)
        # Nobody is at the keyboard: every typed question sees input end.
        monkeypatch.setattr(r, "_read_line", lambda label: None)
        try:
            yield r
        finally:
            for sid, _cmd, _status in agent.shells.list():
                agent.shells.kill(sid)


def test_every_slash_command_survives_a_populated_session(repl):
    assert repl.agent.todos.items and repl.agent.checkpoints.list_groups()
    failed = []
    for line in COMMANDS:
        try:
            repl._handle_slash(line)
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            frames = [f for f in traceback.extract_tb(exc.__traceback__)
                      if "tests" not in f.filename and "site-packages" not in f.filename]
            where = " < ".join(f"{os.path.basename(f.filename)}:{f.lineno}" for f in frames[-3:])
            failed.append(f"{line}: {type(exc).__name__}: {exc} (at {where})")
    assert not failed, "\n".join(failed)


def test_bracketed_text_is_shown_not_swallowed(repl):
    for line in ("/help", "/budget x", "/rewind x", "/permissions wat", "/skill", "/memory wat",
                 "/memory add convention: tabs here [/k] [bold]", "/memory list", "/commands",
                 "/shells", "/tasks", "/rewind", "/timeline", "/dashboard", "/checkpoints"):
        repl._handle_slash(line)
    text = " ".join(ui.console.export_text().split())
    for want in ("/model [name]", "/route [on|off]", "[dollars|off]", "/rewind [n] [--keep-files]",
                 "[allow|deny|remove <rule>]", "/skill <name> [task]", "[list|add <note>",
                 "tabs here [/k] [bold]", "say hi [/j]", "[name]", "sleep 30 # [/d] [bold]",
                 "search the api [/g]", "change a.py [/e] and app/[slug]/page.py"):
        assert want in text, want


def test_mid_turn_slash_suggestions_cover_every_command(repl):
    from coded.repl import SLASH_COMMANDS

    names = set(repl.suggest.registry())
    assert set(SLASH_COMMANDS) <= names and "/hello" in names and "/planner" in names
    for name in SLASH_COMMANDS:
        assert name in [s.display for s in repl.suggest.suggest(name, limit=50)], name
    assert repl.suggest.suggest("/dashb")[0].text == "/dashboard "
    assert repl.suggest.suggest("hello") == []


def test_a_skill_runs_as_its_own_slash_command(repl, monkeypatch):
    sent = []
    monkeypatch.setattr(repl.agent, "run", lambda msg, **kw: sent.append(msg) or "ok")
    repl._handle_slash("/planner write a mail")
    assert sent and sent[0].startswith("Apply the skill 'planner'")
    assert "for this task: write a mail" in sent[0]
    top = repl.suggest.suggest("/plann")[0]
    assert top.text == "/planner " and top.source == "skill"


def test_slash_mission_runs_a_mission_for_this_session(repl, monkeypatch):
    import coded.mission as mission

    seen = []
    monkeypatch.setattr(mission, "mission_main", lambda argv: seen.append(argv) or 0)
    repl._handle_slash("/mission don't break the [/x] build --budget 2")
    argv = seen[0]
    assert "don't" in argv and "--budget" in argv and "2" in argv
    assert argv[argv.index("--cwd") + 1] == repl.agent.cwd
