"""Tests for the git tool and the github tool (against a fake API)."""

from __future__ import annotations

import json
import subprocess

from coded.permissions import Decision, PermissionManager
from coded.tools import build_registry
from coded.tools.base import ToolContext


def _git_repo(tmp_path):
    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("hello\n")
    run("add", ".")
    run("commit", "-qm", "initial")
    return tmp_path


def test_git_read_only_runs_without_permission(tmp_path):
    _git_repo(tmp_path)
    # prompt that would DENY — but read-only must not consult it.
    perms = PermissionManager(auto_approve=False, prompt=lambda t, d: "no")
    ctx = ToolContext(cwd=str(tmp_path), permissions=perms, config=None)
    res = build_registry().get("git").run({"args": "status"}, ctx)
    assert not res.is_error
    assert "branch" in res.content.lower() or "clean" in res.content.lower()


def test_git_mutating_is_gated(tmp_path):
    _git_repo(tmp_path)
    perms = PermissionManager(auto_approve=False, prompt=lambda t, d: "no")
    ctx = ToolContext(cwd=str(tmp_path), permissions=perms, config=None)
    res = build_registry().get("git").run({"args": "checkout -b newbranch"}, ctx)
    assert res.is_error and "denied" in res.content.lower()
    # branch should NOT have been created
    branches = subprocess.run(["git", "branch"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "newbranch" not in branches


def test_github_list_and_create_pr(tmp_path, route_server, monkeypatch):
    created = {}

    def route(method, path, body):
        path = path.split("?")[0]
        if method == "GET" and path.endswith("/pulls"):
            prs = [{"number": 1, "title": "Fix bug",
                    "head": {"ref": "fix"}, "base": {"ref": "main"}}]
            return 200, "application/json", json.dumps(prs)
        if method == "POST" and path.endswith("/pulls"):
            created["body"] = json.loads(body)
            return 201, "application/json", json.dumps(
                {"number": 42, "html_url": "http://x/pull/42"})
        return 404, "application/json", "{}"

    with route_server(route) as srv:
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        monkeypatch.setenv("GITHUB_API_URL", srv.base)
        perms = PermissionManager(auto_approve=True)
        ctx = ToolContext(cwd=str(tmp_path), permissions=perms, config=None)
        tool = build_registry().get("github")

        listed = tool.run({"action": "list_prs", "repo": "o/r"}, ctx)
        assert "#1 Fix bug" in listed.content

        made = tool.run({"action": "create_pr", "repo": "o/r", "title": "T",
                         "head": "feature", "base": "main"}, ctx)
        assert "Created PR #42" in made.content
        assert created["body"]["head"] == "feature"


def test_github_needs_token(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True), config=None)
    res = build_registry().get("github").run({"action": "list_prs", "repo": "o/r"}, ctx)
    assert res.is_error and "GITHUB_TOKEN" in res.content
