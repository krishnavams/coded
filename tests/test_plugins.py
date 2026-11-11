"""Skill/plugin installation, marketplaces, and how plugins plug into coded."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from coded.commands import discover_commands
from coded.config import load_config
from coded.plugins import (
    InstallError,
    add_marketplace,
    install_plugin,
    install_skills,
    installed_plugins,
    list_skills,
    remove_plugin,
    remove_skill,
    set_plugin_enabled,
    update_plugins,
    update_skills,
)
from coded.skills import discover_skills
from coded.shells import shell_path
from coded.sources import SourceError, parse_source, safe_extract_zip

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
YES = lambda summary: True  # noqa: E731


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated user config dir + a project dir."""
    cfg = tmp_path / "userhome" / "config.json"
    monkeypatch.setenv("CODED_CONFIG", str(cfg))
    project = tmp_path / "project"
    project.mkdir()
    return cfg.parent, project


def _skill(dirpath: Path, name: str, desc: str = "Does a thing. Use when asked.", extra=None):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nBody of {name}.\n")
    for rel, text in (extra or {}).items():
        (dirpath / rel).write_text(text)
    return dirpath


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
def test_parse_source_kinds(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    assert parse_source(str(d)).type == "dir"
    gh = parse_source("acme/tools/skills/lint@v2")
    assert (gh.type, gh.location, gh.subpath, gh.ref) == (
        "github", "https://github.com/acme/tools.git", "skills/lint", "v2")
    assert parse_source("https://x.org/a.git").type == "git"
    assert parse_source("git@github.com:a/b.git").type == "git"
    assert parse_source("https://x.org/p.zip").type == "zip-url"
    with pytest.raises(SourceError):
        parse_source("--upload-pack=evil")
    with pytest.raises(SourceError):
        parse_source("./does/not/exist")


def test_zip_slip_and_symlinks_rejected(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(SourceError, match="Unsafe path"):
        safe_extract_zip(bad, tmp_path / "out1")
    link = tmp_path / "link.zip"
    with zipfile.ZipFile(link, "w") as zf:
        info = zipfile.ZipInfo("evil")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    (tmp_path / "out2").mkdir()
    with pytest.raises(SourceError, match="symlink"):
        safe_extract_zip(link, tmp_path / "out2")
    assert not (tmp_path / "escape.txt").exists()


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------
def test_install_skill_from_dir_list_remove(home, tmp_path):
    user, project = home
    src = _skill(tmp_path / "src" / "lint", "lint", extra={"run.sh": "echo hi"})
    os.chmod(src / "run.sh", 0o755)
    seen = []
    names = install_skills(str(src), cwd=str(project), confirm=lambda s: seen.append(s) or True)
    assert names == ["lint"]
    assert "run.sh  (executable)" in seen[0]
    assert (user / "skills" / "lint" / "SKILL.md").is_file()
    assert "lint" in discover_skills(str(project))
    rows = list_skills(str(project))
    assert rows[0]["name"] == "lint" and str(src) in rows[0]["source"]
    remove_skill("lint", cwd=str(project))
    assert "lint" not in discover_skills(str(project))


def test_install_skill_from_zip_with_wrapper_dir(home, tmp_path):
    user, project = home
    archive = tmp_path / "skill.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("repo-main/SKILL.md", "---\nname: zipped\ndescription: From a zip.\n---\nHi\n")
    assert install_skills(str(archive), cwd=str(project), confirm=YES) == ["zipped"]
    assert (user / "skills" / "zipped" / "SKILL.md").is_file()


def test_multi_skill_source_needs_selection_and_validation(home, tmp_path):
    _, project = home
    pack = tmp_path / "pack"
    _skill(pack / "skills" / "one", "one")
    _skill(pack / "skills" / "two", "two")
    with pytest.raises(InstallError, match="several skills"):
        install_skills(str(pack), cwd=str(project), confirm=YES)
    assert install_skills(str(pack), names=["two"], cwd=str(project), confirm=YES) == ["two"]
    assert install_skills(str(pack), install_all=True, cwd=str(project), confirm=YES,
                          force=True) == ["one", "two"]
    nodesc = tmp_path / "nodesc"
    nodesc.mkdir()
    (nodesc / "SKILL.md").write_text("---\nname: nodesc\n---\nx")
    with pytest.raises(InstallError, match="no description"):
        install_skills(str(nodesc), cwd=str(project), confirm=YES)


def test_install_cancelled_and_project_scope(home, tmp_path):
    user, project = home
    src = _skill(tmp_path / "s", "proj-skill")
    with pytest.raises(InstallError, match="Cancelled"):
        install_skills(str(src), cwd=str(project), confirm=lambda s: False)
    assert not (user / "skills" / "proj-skill").exists()
    install_skills(str(src), scope="project", cwd=str(project), confirm=YES)
    assert (project / ".coded" / "skills" / "proj-skill").is_dir()


def test_hand_copied_skill_needs_force(home, tmp_path):
    user, project = home
    _skill(user / "skills" / "mine", "mine")
    src = _skill(tmp_path / "s" / "mine", "mine")
    with pytest.raises(InstallError, match="--force"):
        install_skills(str(src), cwd=str(project), confirm=YES)
    install_skills(str(src), cwd=str(project), confirm=YES, force=True)


def test_skill_from_git_and_update(home, tmp_path):
    _, project = home
    repo = tmp_path / "repo"
    _skill(repo, "gitskill", "Version one.")
    _git("init", "-q", cwd=repo)
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "v1", cwd=repo)
    install_skills(f"file://{repo}", cwd=str(project), confirm=YES)
    assert "Version one." in discover_skills(str(project))["gitskill"].description
    _skill(repo, "gitskill", "Version two.")
    _git("commit", "-qam", "v2", cwd=repo)
    assert update_skills(cwd=str(project), confirm=YES) == [("gitskill", "updated")]
    assert "Version two." in discover_skills(str(project))["gitskill"].description


def test_failed_update_keeps_old_copy(home, tmp_path):
    user, project = home
    src = _skill(tmp_path / "s", "keep")
    install_skills(str(src), cwd=str(project), confirm=YES)
    shutil.rmtree(src)
    [(name, result)] = update_skills(cwd=str(project), confirm=YES)
    assert result.startswith("failed") and (user / "skills" / "keep" / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------
def _plugin(root: Path, name="toolkit", fmt=".coded-plugin", hooks=None, mcp=None, manifest=None):
    (root / fmt).mkdir(parents=True)
    (root / fmt / "plugin.json").write_text(json.dumps({"name": name, "version": "0.1.0",
                                                         "description": "Test plugin", **(manifest or {})}))
    _skill(root / "skills" / "deploy-check", "deploy-check")
    (root / "commands").mkdir()
    (root / "commands" / "ship.md").write_text("---\ndescription: Ship it\n---\nShip $ARGUMENTS")
    if hooks is not None:
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    if mcp is not None:
        (root / ".mcp.json").write_text(json.dumps(mcp))
    return root


def test_plugin_install_contributes_everything_and_disable_removes_it(home, tmp_path):
    user, project = home
    (user).mkdir(parents=True, exist_ok=True)
    (user / "config.json").write_text(json.dumps({"hooks": {"PostToolUse": [{"command": "echo user"}]}}))
    src = _plugin(tmp_path / "p", hooks={"PostToolUse": [{"matcher": "write", "hooks": [
        {"type": "command", "command": "${CODED_PLUGIN_ROOT}/hooks/check.sh"}]}]},
        mcp={"mcpServers": {"db": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/db-mcp", "args": ["--ro"]}}},
        manifest={"external_agents": {"helper": {"command": ["helper", "{prompt}"]}}})
    summaries = []
    plugin = install_plugin(str(src), cwd=str(project), confirm=lambda s: summaries.append(s) or True)
    assert "HOOKS — these commands run automatically" in summaries[0]
    assert "${PLUGIN_ROOT}/hooks/check.sh" in summaries[0]
    root = user / "plugins" / "toolkit"
    assert plugin.root == root

    assert "deploy-check" in discover_skills(str(project))
    cmds = discover_commands(str(project))
    assert cmds["toolkit:ship"].expand("now") == "Ship now"
    cfg = load_config(cwd=str(project))
    post = cfg.hooks["PostToolUse"]
    assert post[0]["command"] == "echo user"  # user hook kept, plugin hook appended
    # Hook commands run through a shell, so the root is written the way that
    # shell reads paths (forward slashes under Git Bash on Windows).
    assert post[1]["command"] == f"{shell_path(root)}/hooks/check.sh"
    assert post[1]["env"]["CODED_PLUGIN_ROOT"] == str(root)
    assert cfg.mcp_servers["toolkit-db"]["command"] == f"{root}/bin/db-mcp"
    assert "helper" in cfg.external_agents and cfg.plugins == ["toolkit"]

    set_plugin_enabled("toolkit", False, cwd=str(project))
    assert "deploy-check" not in discover_skills(str(project))
    assert "toolkit:ship" not in discover_commands(str(project))
    cfg = load_config(cwd=str(project))
    assert len(cfg.hooks["PostToolUse"]) == 1 and "toolkit-db" not in cfg.mcp_servers
    set_plugin_enabled("toolkit", True, cwd=str(project))
    remove_plugin("toolkit", cwd=str(project))
    assert not root.exists() and installed_plugins(str(project), include_disabled=True) == []


def test_claude_format_plugin_maps_tools_and_warns(home, tmp_path):
    _, project = home
    src = _plugin(tmp_path / "cp", name="claude-style", fmt=".claude-plugin", hooks={
        "PreToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "guard"}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": "bye"}]}],
    }, mcp={"remote": {"type": "http", "url": "https://x"}})
    (src / "agents").mkdir()
    (src / "agents" / "reviewer.md").write_text("---\nname: r\n---\n")
    summary = []
    p = install_plugin(str(src), cwd=str(project), confirm=lambda s: summary.append(s) or True)
    assert p.format == "claude"
    assert p.hooks()["PreToolUse"][0]["matcher"] == "edit|multi_edit|apply_patch|write"
    text = summary[0]
    assert "SessionEnd" in text and "agents/" in text and "stdio" in text


def test_real_claude_hook_output_is_understood(tmp_path):
    from coded.hooks import HookRunner

    out = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": "nope"}})
    runner = HookRunner({"PreToolUse": [{"command": f"echo '{out}'"}]}, str(tmp_path))
    res = runner.run("PreToolUse", {}, tool_name="bash")
    assert res.blocked and res.message == "nope"
    ctx = json.dumps({"hookSpecificOutput": {"additionalContext": "remember X"}})
    runner = HookRunner({"SessionStart": [{"command": f"echo '{ctx}'"}]}, str(tmp_path))
    assert runner.run("SessionStart").context == ["remember X"]


def test_plugin_hook_env_reaches_command(tmp_path):
    from coded.hooks import HookRunner

    runner = HookRunner({"Stop": [{"command": "echo $CODED_PLUGIN_ROOT", "env": {"CODED_PLUGIN_ROOT": "/pr"}}]},
                        str(tmp_path))
    assert runner.run("Stop").context == ["/pr"]


def test_plugin_requires_manifest_and_valid_name(home, tmp_path):
    _, project = home
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(InstallError, match="manifest"):
        install_plugin(str(empty), cwd=str(project), confirm=YES)
    bad = tmp_path / "bad"
    (bad / ".coded-plugin").mkdir(parents=True)
    (bad / ".coded-plugin" / "plugin.json").write_text('{"name": "Bad Name"}')
    with pytest.raises(InstallError, match="Invalid plugin name"):
        install_plugin(str(bad), cwd=str(project), confirm=YES)


def test_plugin_update_from_git(home, tmp_path):
    _, project = home
    repo = _plugin(tmp_path / "prepo", name="evolving")
    _git("init", "-q", cwd=repo)
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "v1", cwd=repo)
    install_plugin(f"file://{repo}", cwd=str(project), confirm=YES)
    with pytest.raises(InstallError, match="already installed"):
        install_plugin(f"file://{repo}", cwd=str(project), confirm=YES)
    (repo / "commands" / "new.md").write_text("New command")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "v2", cwd=repo)
    assert update_plugins(cwd=str(project), confirm=YES) == [("evolving", "updated to 0.1.0")]
    assert "evolving:new" in discover_commands(str(project))


def test_example_plugin_and_marketplace(home, tmp_path):
    user, project = home
    name, count = add_marketplace(str(EXAMPLES), cwd=str(project))
    assert (name, count) == ("coded-examples", 1)
    p = install_plugin("code-quality@coded-examples", cwd=str(project), confirm=YES)
    assert p.name == "code-quality"
    assert "changelog" in discover_skills(str(project))
    assert "code-quality:tidy" in discover_commands(str(project))
    rec = json.loads((user / "installed.json").read_text())["plugins"]["code-quality"]
    assert rec["marketplace"] == "coded-examples"
    [(n, result)] = update_plugins(cwd=str(project), confirm=YES)
    assert result.startswith("updated")


def test_example_hook_flags_trailing_whitespace(home, tmp_path, fake_server):
    from coded.agent import create_agent
    from coded.config import ModelConfig
    from coded.permissions import PermissionManager

    _, project = home
    install_plugin(str(EXAMPLES / "plugins" / "code-quality"), cwd=str(project), confirm=YES)
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    calls = {"n": 0}

    def script(body):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function", "function": {
                    "name": "write", "arguments": json.dumps({"path": "a.py", "content": "x = 1   \n"})}}]}, usage
        return {"role": "assistant", "content": "ok"}, usage

    with fake_server(script) as srv:
        cfg = load_config(cwd=str(project))
        model = ModelConfig(name="f", model="f", provider="openai-compatible",
                            base_url=srv.base_url, api_key="x")
        cfg.add_model(model)
        agent = create_agent(model=model, config=cfg, cwd=str(project),
                             permissions=PermissionManager(auto_approve=True), stream=False, verbose=False)
        agent.run("write it")
    tool_msg = [m for m in agent.session.messages if m.get("role") == "tool"][0]["content"]
    assert "trailing whitespace on line(s) 1" in tool_msg


def test_cli_refuses_without_tty_or_yes(home, tmp_path, monkeypatch, capsys):
    from coded.cli import main

    _, project = home
    src = _skill(tmp_path / "s", "cli-skill")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["skill", "install", str(src), "--cwd", str(project)]) == 1
    assert main(["skill", "install", str(src), "--cwd", str(project), "--yes"]) == 0
    assert main(["skill", "list", "--cwd", str(project)]) == 0
    assert main(["plugin", "install", str(EXAMPLES / "plugins" / "code-quality"), "-y",
                 "--cwd", str(project)]) == 0
    assert main(["plugin", "list", "--cwd", str(project)]) == 0
    assert main(["plugin", "disable", "code-quality", "--cwd", str(project)]) == 0
    assert main(["plugin", "info", "code-quality", "--cwd", str(project)]) == 0
    assert main(["plugin", "marketplace", "add", str(EXAMPLES)]) == 0
    assert main(["plugin", "search", "quality"]) == 0
    assert main(["plugin", "remove", "code-quality", "--cwd", str(project)]) == 0
    assert main(["skill", "remove", "cli-skill", "--cwd", str(project)]) == 0


def test_symlinks_in_sources_are_refused(home, tmp_path):
    _, project = home
    src = _plugin(tmp_path / "linky", name="linky")
    os.symlink(Path.home(), src / "evil")
    with pytest.raises(SourceError, match="symlink"):
        install_plugin(str(src), cwd=str(project), confirm=YES)
    _git("init", "-q", cwd=src)
    _git("add", ".", cwd=src)
    _git("commit", "-qm", "x", cwd=src)
    with pytest.raises(SourceError, match="symlink"):
        install_plugin(f"file://{src}", cwd=str(project), confirm=YES)
    assert not (home[0] / "plugins").exists()


def test_plugin_mcp_server_launches_with_plugin_root(home, tmp_path):
    pytest.importorskip("mcp")
    import sys

    from coded.mcp_client import MCPManager

    _, project = home
    src = tmp_path / "mcp-plugin"
    (src / ".claude-plugin").mkdir(parents=True)
    (src / ".claude-plugin" / "plugin.json").write_text('{"name": "rootcheck"}')
    (src / "server.py").write_text(
        "import os\n"
        "try:\n    from mcp.server.mcpserver import MCPServer as S\n"
        "except ImportError:\n    from mcp.server.fastmcp import FastMCP as S\n"
        "s = S('rootcheck')\n"
        "@s.tool()\ndef where() -> str:\n    return os.environ.get('CLAUDE_PLUGIN_ROOT', 'unset')\n"
        "s.run()\n")
    (src / ".mcp.json").write_text(json.dumps({"mcpServers": {"srv": {
        "command": sys.executable, "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"]}}}))
    plugin = install_plugin(str(src), cwd=str(project), confirm=YES)
    cfg = load_config(cwd=str(project))
    manager = MCPManager({"rootcheck-srv": cfg.mcp_servers["rootcheck-srv"]})
    manager.start()
    try:
        assert not manager.errors, manager.errors
        assert manager.call("rootcheck-srv", "where", {}).strip() == str(plugin.root)
    finally:
        manager.stop()
