"""`coded import`: MCP servers from other agents' configs → coded's config."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from coded import agent_import as ai
from coded.config_wizard import Prompter
from coded.mcp_client import expand_refs, server_params


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home, proj = tmp_path / "home", tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("CODED_CONFIG", str(home / ".config/coded/config.json"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # Claude Code: user scope, this project's local scope, another project's.
    _write(home / ".claude.json", {
        "mcpServers": {"github": {"command": "npx", "args": ["-y", "gh-mcp"],
                                  "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}},
        "projects": {str(proj): {"mcpServers": {"local": {"command": "uvx", "args": ["x"]}}},
                     "/elsewhere": {"mcpServers": {"other": {"command": "nope"}}}},
    })
    _write(proj / ".mcp.json", {"mcpServers": {
        "remote": {"type": "http", "url": "https://mcp.example.com/mcp"}}})
    _write(home / ".cursor/mcp.json", {"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "gh-mcp"],
                   "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},   # same as Claude's: deduped
        "db": {"command": "db-mcp", "env": {"DB_PASSWORD": "hunter2"}}}})
    _write(proj / ".vscode/mcp.json", {"servers": {
        "fs": {"type": "stdio", "command": "fs-mcp", "env": {"ROOT": "${env:HOME}"}},
        "asks": {"command": "a", "env": {"KEY": "${input:key}"}}}})
    _write(home / ".gemini/settings.json", {"mcpServers": {
        "github": {"command": "other-gh"}}})   # clashes with Claude's github
    _write(home / ".codex/config.toml",
           '[mcp_servers.docs]\ncommand = "docs-mcp"\nargs = ["--fast"]\n')
    return home, proj


def _run(setup, argv, answers=(), interactive=True):
    home, proj = setup
    lines = []
    replies = iter(answers)
    prompter = Prompter(ask=lambda q, d: next(replies), secret=lambda q: "",
                        out=lines.append)
    code = ai.main(argv, cwd=str(proj), home=home, prompter=prompter,
                   interactive=interactive, out=lines.append)
    return code, "\n".join(lines)


def _by_name(found):
    return {f.name + ":" + f.agent: f for f in found}


def test_discover_reads_every_agents_format(setup):
    home, proj = setup
    found, problems = ai.discover(str(proj), home=home)
    got = _by_name(found)
    assert not problems
    assert got["github:claude"].entry == {"command": "npx", "args": ["-y", "gh-mcp"],
                                          "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}
    assert "local:claude" in got and not any(f.name == "other" for f in found)
    assert "github:cursor" not in got            # identical copy, listed once
    assert "a URL server" in got["remote:claude"].skip
    assert got["fs:vscode"].entry["env"] == {"ROOT": "${HOME}"}
    assert "${input:" in got["asks:vscode"].skip
    assert "taken by the claude server" in got["github:gemini"].skip
    if sys.version_info >= (3, 11):
        assert got["docs:codex"].entry == {"command": "docs-mcp", "args": ["--fast"]}


def test_list_changes_nothing(setup):
    code, out = _run(setup, ["--list"])
    assert code == 0 and "runs: npx -y gh-mcp" in out and "hunter2" not in out
    assert "DB_PASSWORD=<value copied>" in out
    assert not os.path.exists(os.environ["CODED_CONFIG"])


def test_import_chosen_servers_into_the_user_config(setup):
    path = os.environ["CODED_CONFIG"]
    _write(__import__("pathlib").Path(path), {"models": {}, "mcp_servers": {
        "local": {"command": "mine"}}})
    code, out = _run(setup, ["--from", "claude,cursor"], answers=["*"])
    assert code == 0, out
    data = json.load(open(path))
    assert data["mcp_servers"]["local"] == {"command": "mine"}   # never overwritten
    assert "already in this coded config" in out
    assert set(data["mcp_servers"]) == {"local", "github", "db"}
    assert data["mcp_servers"]["db"]["env"] == {"DB_PASSWORD": "hunter2"}
    assert "models" in data                                       # other keys kept
    if os.name != "nt":
        assert os.stat(path).st_mode & 0o777 == 0o600             # it holds a secret


def test_project_config_refuses_literal_secrets(setup):
    home, proj = setup
    code, out = _run(setup, ["--project", "--from", "cursor,claude", "-y"])
    assert code == 0, out
    data = json.loads((proj / ".coded/config.json").read_text())
    assert "github" in data["mcp_servers"] and "db" not in data["mcp_servers"]
    assert "literal values (DB_PASSWORD)" in out


def test_non_interactive_needs_yes_and_only_filters(setup):
    code, out = _run(setup, [], interactive=False)
    assert code == 1 and "--yes" in out
    code, out = _run(setup, ["--only", "local", "-y"])
    assert code == 0
    assert list(json.load(open(os.environ["CODED_CONFIG"]))["mcp_servers"]) == ["local"]


def test_declining_writes_nothing_and_bad_agent_is_an_error(setup):
    code, out = _run(setup, ["--from", "claude"], answers=[""])
    assert code == 0 and "Nothing imported" in out
    assert not os.path.exists(os.environ["CODED_CONFIG"])
    assert _run(setup, ["--from", "emacs"])[0] == 2


def test_unreadable_files_are_reported_not_fatal(setup):
    home, proj = setup
    _write(home / ".cursor/mcp.json", "{not json")
    found, problems = ai.discover(str(proj), ["cursor", "claude"], home=home)
    assert any("cursor" in p and "not valid JSON" in p for p in problems)
    assert any(f.name == "github" for f in found)


def test_mcp_client_expands_env_references():
    env = {"TOKEN": "abc", "PATH": "/bin"}
    assert expand_refs("${TOKEN}", env) == "abc"
    assert expand_refs("${MISSING:-dflt}", env) == "dflt"
    assert expand_refs("${MISSING}", env) == ""
    assert expand_refs("plain $TOKEN", env) == "plain $TOKEN"
    cmd, args, full = server_params({"command": "x", "args": ["--t=${TOKEN}"],
                                     "env": {"GITHUB_TOKEN": "${TOKEN}"}}, env)
    assert (cmd, args) == ("x", ["--t=abc"]) and full == {**env, "GITHUB_TOKEN": "abc"}
    assert server_params({"command": "x"}, env)[2] is None


def test_cli_dispatches_import(setup, monkeypatch):
    from coded import cli

    home, proj = setup
    monkeypatch.chdir(proj)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert cli.main(["import", "--list"]) == 0


# -- Claude Code slash commands ----------------------------------------------------
FIX = """---
description: Fix an issue
argument-hint: <n>
model: haiku
allowed-tools: Bash(git:*)
---
Fix issue #$1. Status: !`git status`
"""


@pytest.fixture
def commands(setup):
    home, proj = setup
    _write(home / ".claude/commands/fix-issue.md", FIX)
    # Bytes, not text: text mode on Windows would turn \r\n into \r\r\n here.
    (home / ".claude/commands/git").mkdir(parents=True, exist_ok=True)
    (home / ".claude/commands/git/pr.md").write_bytes(b"Open a PR titled $ARGUMENTS\r\n")
    _write(home / ".claude/commands/review.md", "Shadowed by the built-in /review\n")
    _write(proj / ".claude/commands/deploy.md", "---\nmodel: smart\n---\nDeploy $1\n")
    _write(proj / ".claude/commands/git/pr.md", "the project's own pr\n")
    return home, proj


def _user_cmds():
    return Path(os.environ["CODED_CONFIG"]).parent / "commands"


def test_commands_are_copied_with_their_scope(setup, commands):
    home, proj = commands
    # `smart` is a coded model here, so the project command keeps its model line.
    _write(Path(os.environ["CODED_CONFIG"]), {"models": {"smart": {"model": "x", "base_url": "http://x/v1"}}})
    code, out = _run(setup, ["commands", "-y"])
    assert code == 0, out
    fix = (_user_cmds() / "fix-issue.md").read_text()
    assert "model:" not in fix and "description: Fix an issue" in fix and "!`git status`" in fix
    assert "`model: haiku` is not a coded model" in out
    assert "has !`…` lines" in out and "`allowed-tools` is ignored" in out
    assert (_user_cmds() / "git/pr.md").read_bytes() == b"Open a PR titled $ARGUMENTS\r\n"
    assert (proj / ".coded/commands/deploy.md").read_text() == "---\nmodel: smart\n---\nDeploy $1\n"
    assert (proj / ".coded/commands/git/pr.md").read_text() == "the project's own pr\n"
    assert not (_user_cmds() / "review.md").exists()
    assert "coded has a built-in /review" in out
    # coded reads them back under the names the summary showed.
    from coded.commands import discover_commands
    found = discover_commands(str(proj))
    assert {"fix-issue", "git:pr", "deploy"} <= set(found)
    assert found["fix-issue"].model is None and found["deploy"].model == "smart"
    # A second run changes nothing.
    code, out = _run(setup, ["commands", "-y"])
    assert "already imported" in out and "Nothing to import" in out
    # CRLF files included: every one of them is recognised, none reported as a clash.
    assert "a coded command with this name exists" not in out


def test_project_flag_and_conflicts(setup, commands):
    home, proj = commands
    _write(proj / ".coded/commands/deploy.md", "mine\n")
    code, out = _run(setup, ["commands", "--project", "-y"])
    assert code == 0, out
    assert (proj / ".coded/commands/fix-issue.md").exists()          # user one, into project
    assert not _user_cmds().exists()
    assert (proj / ".coded/commands/deploy.md").read_text() == "mine\n"   # never overwritten
    assert "a coded command with this name exists" in out
    # user git/pr went first; the project's git/pr would land on the same file
    assert "the name is taken by" in out


def test_choosing_listing_and_filters(setup, commands):
    home, proj = commands
    code, out = _run(setup, ["--list"])
    assert code == 0 and "/fix-issue" in out and "github" in out    # both kinds shown
    assert not _user_cmds().exists()
    code, out = _run(setup, ["--only", "/git:pr,local"], answers=["*"])
    assert code == 0, out
    assert (_user_cmds() / "git/pr.md").exists() and not (_user_cmds() / "fix-issue.md").exists()
    assert list(json.load(open(os.environ["CODED_CONFIG"]))["mcp_servers"]) == ["local"]
    code, out = _run(setup, ["mcp", "--list"])
    assert "/fix-issue" not in out
    code, out = _run(setup, ["commands", "--from", "cursor"])
    assert "No Claude Code commands found" in out
