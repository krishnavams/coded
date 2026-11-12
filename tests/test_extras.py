"""Output styles, the custom status line, coded doctor, the update notice,
the browser tool and the GitHub Actions example."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.tools.base import ToolContext


def _agent(tmp_path, **cfg_kw):
    cfg = Config(**cfg_kw)
    m = ModelConfig(name="m", model="m", base_url="http://127.0.0.1:9/v1", api_key="k")
    cfg.add_model(m, make_default=True)
    return create_agent(model=m, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(), stream=False, verbose=False)


# -- output styles ---------------------------------------------------------------

def test_styles_are_found_and_reach_the_system_prompt(tmp_path, monkeypatch):
    from coded.styles import discover_styles

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / ".coded" / "styles").mkdir(parents=True)
    (tmp_path / ".coded" / "styles" / "pirate.md").write_text(
        "---\ndescription: Talks like a pirate.\n---\nSay arr.\n")
    styles = discover_styles(str(tmp_path))
    assert {"explanatory", "concise", "learning", "pirate"} <= set(styles)
    assert styles["pirate"].description == "Talks like a pirate."

    plain = _agent(tmp_path).session.messages[0]["content"]
    styled = _agent(tmp_path, output_style="pirate").session.messages[0]["content"]
    assert "# Output style: pirate" in styled and "Say arr." in styled
    assert "Output style" not in plain
    unknown = _agent(tmp_path, output_style="nope").session.messages[0]["content"]
    assert unknown == plain  # an unknown style changes nothing


def test_style_command_switches_and_lists(tmp_path, headless, capsys):
    from coded.repl import Repl

    agent = _agent(tmp_path)
    repl = Repl(agent, agent.config)
    repl._handle_slash("/style concise")
    assert agent.config.output_style == "concise"
    assert "# Output style: concise" in agent.session.messages[0]["content"]
    capsys.readouterr()
    repl._handle_slash("/style")
    out = capsys.readouterr().out
    assert "● concise" in out and "explanatory" in out
    repl._handle_slash("/style off")
    assert agent.config.output_style is None
    assert "Output style" not in agent.session.messages[0]["content"]


# -- status line --------------------------------------------------------------------

class _Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_status_line_runs_the_command_with_json_and_caches(tmp_path):
    from coded.statusline import StatusLine, status_data

    agent = _agent(tmp_path)
    seen = []

    def run(cmd, input=None, timeout=None):
        seen.append(json.loads(input))
        return SimpleNamespace(returncode=0, stdout=f"\x1b[35mbranch main\x1b[0m · {len(seen)}\nx")

    clock = _Clock()
    line = StatusLine({"command": "status.sh", "refresh": 5}, lambda: status_data(agent),
                      run=run, clock=clock, background=False)
    assert line.text() == "\x1b[35mbranch main\x1b[0m · 1"
    assert seen[0]["model"] == "m" and seen[0]["mode"] == "default" and "tasks" in seen[0]
    clock.t = 3
    assert line.text().endswith("· 1") and len(seen) == 1   # cached
    clock.t = 6
    assert line.text().endswith("· 2")

    failing = StatusLine({"command": "boom"}, dict, background=False,
                         run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="x"))
    assert failing.text() is None   # the built-in line is shown
    assert StatusLine({}, dict).text() is None


def test_repl_shows_the_custom_status_line(tmp_path, headless):
    from prompt_toolkit.formatted_text import to_formatted_text

    from coded.repl import Repl

    agent = _agent(tmp_path, statusline={"command": "x"})
    repl = Repl(agent, agent.config)
    repl.statusline._text, repl.statusline._last = "my status", float("inf")
    text = "".join(t[1] for t in to_formatted_text(repl._bottom_toolbar()))
    assert text.strip() == "my status"


# -- doctor ------------------------------------------------------------------------

def test_doctor_reports_each_area(tmp_path, monkeypatch):
    from coded.doctor import FAIL, OK, render, run_checks

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".coded").mkdir()
    (tmp_path / ".coded" / "config.json").write_text(json.dumps({
        "models": {"local": {"provider": "ollama", "model": "qwen"},
                   "cloud": {"provider": "openai", "model": "gpt-4o"}},
        "default_model": "local"}))
    checks = {c.area: c for c in run_checks(str(tmp_path), which=lambda n: None,
                                            isatty=lambda: False)}
    assert checks["Python"].status == OK and checks["openai"].status == OK
    assert checks["model local"].status == OK
    assert checks["model cloud"].status == FAIL and "OPENAI_API_KEY" in checks["model cloud"].detail
    assert checks["git"].status != OK and checks["terminal"].status != OK
    text = render(list(checks.values()))
    assert "✗ model cloud" in text and "problem(s)" in text


def test_doctor_command_exit_code(tmp_path, monkeypatch, capsys):
    from coded.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = main(["doctor", "--cwd", str(tmp_path)])
    out = capsys.readouterr().out
    assert "models" in out and code == 1   # no model configured is a problem


def test_broken_config_is_reported(tmp_path, monkeypatch):
    from coded.doctor import FAIL, run_checks

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / ".coded").mkdir()
    (tmp_path / ".coded" / "config.json").write_text("{not json")
    config = next(c for c in run_checks(str(tmp_path), isatty=lambda: False)
                  if c.area == "config")
    assert config.status == FAIL


# -- update notice ---------------------------------------------------------------------

def test_update_check_compares_with_the_remote(tmp_path, monkeypatch):
    from coded import update_check as uc

    def git(local, remote, have_remote):
        def run(argv, **kw):
            args = argv[1:]
            if args[:2] == ["rev-parse", "HEAD"]:
                return SimpleNamespace(returncode=0, stdout=local)
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return SimpleNamespace(returncode=0, stdout="main")
            if args[0] == "ls-remote":
                return SimpleNamespace(returncode=0, stdout=f"{remote}\trefs/heads/main")
            if args[0] == "cat-file":
                return SimpleNamespace(returncode=0 if have_remote else 1, stdout="")
            return SimpleNamespace(returncode=1, stdout="")
        return run

    assert uc.check_now(tmp_path, git("a", "b", False))["behind"] is True
    assert uc.check_now(tmp_path, git("a", "b", True))["behind"] is False
    assert uc.check_now(tmp_path, git("a", "a", True))["behind"] is False

    monkeypatch.delenv("CODED_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    started = []
    assert uc.notice(root=tmp_path, start_check=started.append) is None
    assert started == [tmp_path]          # nothing cached: a check is started
    uc.cache_path().parent.mkdir(parents=True)
    uc.cache_path().write_text(json.dumps({"checked": 9e18, "root": str(tmp_path),
                                           "behind": True, "branch": "main"}))
    msg = uc.notice(root=tmp_path, start_check=started.append, now=lambda: 9e18)
    assert "newer coded" in msg and "git -C" in msg and len(started) == 1
    assert uc.notice(False, root=tmp_path) is None
    monkeypatch.setenv("CODED_NO_UPDATE_CHECK", "1")
    assert uc.notice(root=tmp_path) is None


# -- browser tool -------------------------------------------------------------------------

def test_browser_tool_is_opt_in(tmp_path):
    assert _agent(tmp_path).registry.get("browser") is None
    assert _agent(tmp_path, browser=True).registry.get("browser") is not None
    tool = _agent(tmp_path, browser={"executable_path": "/x/chrome"}).registry.get("browser")
    assert tool.executable_path == "/x/chrome"


def test_browser_tool_checks_input_and_asks_before_acting(tmp_path):
    from coded.tools.browser import BrowserTool

    tool = BrowserTool()
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=Config())
    assert tool.run({"url": "javascript:alert(1)"}, ctx).is_error
    assert "Unknown step" in tool.run({"url": "http://x", "steps": [{"action": "hack"}]},
                                      ctx).content
    denied = tool.run({"url": "http://x", "steps": [{"action": "click", "selector": "a"}]}, ctx)
    assert denied.is_error and "did not allow" in denied.content


def test_browser_tool_renders_a_page(tmp_path):
    pytest.importorskip("playwright")
    from coded.tools.browser import BrowserTool

    page = tmp_path / "app.html"
    page.write_text("""<html><head><title>Demo</title></head><body>
      <input id="name"><button id="go" onclick="
        document.getElementById('out').textContent = 'Hello ' + document.getElementById('name').value;
        console.error('clicked');">Go</button>
      <p id="out"></p><script>document.write('rendered by JS')</script></body></html>""")
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True),
                      config=Config())
    import glob
    import os

    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    exe = os.environ.get("CODED_TEST_CHROME") or (found[-1] if found else "")
    try:
        res = BrowserTool(exe).run({"url": page.as_uri(), "steps": [
            {"action": "fill", "selector": "#name", "value": "Ada"},
            {"action": "click", "selector": "#go"}]}, ctx)
    except Exception as exc:  # pragma: no cover - environment without a browser
        pytest.skip(str(exc))
    if res.is_error and "Chromium is not installed" in res.content:
        pytest.skip(res.content)
    assert not res.is_error, res.content
    assert "Title: Demo" in res.content and "rendered by JS" in res.content
    assert "Hello Ada" in res.content and "error: clicked" in res.content
    shot = res.content.split("Screenshot: ")[1].splitlines()[0]
    assert Path(shot).is_file()


# -- GitHub Actions example -----------------------------------------------------------------

def test_the_github_actions_example_is_valid_and_uses_real_flags():
    yaml = pytest.importorskip("yaml")
    path = Path(__file__).parent.parent / "examples" / "github-actions" / "coded-review.yml"
    wf = yaml.safe_load(path.read_text())
    steps = wf["jobs"]["review"]["steps"]
    run = next(s["run"] for s in steps if s.get("id") == "review")
    for flag in ("--base-url", "--model-name", "--api-key", "--output"):
        assert flag in run
    from coded import cli
    import inspect

    src = inspect.getsource(cli._handle_review_command)
    for flag in ("--base-url", "--model-name", "--api-key", "--output"):
        assert f'"{flag}"' in src
