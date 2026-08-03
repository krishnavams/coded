"""Unit tests for the tool suite and config (no network)."""

from __future__ import annotations

import os

import pytest

from coded.config import ConfigError, ModelConfig, load_config
from coded.permissions import Decision, PermissionManager
from coded.tools import build_registry
from coded.tools.base import ToolContext


def _ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True), config=None)


def _tool(name):
    return build_registry().get(name)


def test_write_read_edit_roundtrip(tmp_path):
    ctx = _ctx(tmp_path)
    w = _tool("write").run({"path": "a.txt", "content": "line1\nline2\n"}, ctx)
    assert not w.is_error
    r = _tool("read").run({"path": "a.txt"}, ctx)
    assert "line1" in r.content and "1\t" in r.content

    e = _tool("edit").run({"path": "a.txt", "old_string": "line1", "new_string": "LINE1"}, ctx)
    assert not e.is_error
    assert (tmp_path / "a.txt").read_text() == "LINE1\nline2\n"


def test_edit_requires_unique_match(tmp_path):
    ctx = _ctx(tmp_path)
    _tool("write").run({"path": "b.txt", "content": "x\nx\n"}, ctx)
    res = _tool("edit").run({"path": "b.txt", "old_string": "x", "new_string": "y"}, ctx)
    assert res.is_error and "not unique" in res.content
    res2 = _tool("edit").run({"path": "b.txt", "old_string": "x", "new_string": "y", "replace_all": True}, ctx)
    assert not res2.is_error
    assert (tmp_path / "b.txt").read_text() == "y\ny\n"


def test_read_missing_file(tmp_path):
    res = _tool("read").run({"path": "nope.txt"}, _ctx(tmp_path))
    assert res.is_error and "not found" in res.content.lower()


def test_grep_and_glob(tmp_path):
    ctx = _ctx(tmp_path)
    (tmp_path / "one.py").write_text("def foo():\n    return 42\n")
    (tmp_path / "two.txt").write_text("nothing here\n")
    g = _tool("glob").run({"pattern": "*.py"}, ctx)
    assert "one.py" in g.content and "two.txt" not in g.content
    gr = _tool("grep").run({"pattern": r"def \w+", "glob": "*.py"}, ctx)
    assert "one.py" in gr.content and "foo" in gr.content


def test_bash_runs_and_reports_exit(tmp_path):
    ctx = _ctx(tmp_path)
    ok = _tool("bash").run({"command": "echo hello"}, ctx)
    assert not ok.is_error and "hello" in ok.content
    bad = _tool("bash").run({"command": "exit 3"}, ctx)
    assert bad.is_error and "exit 3" in bad.content


def test_permission_deny_and_always():
    pm = PermissionManager(auto_approve=False, prompt=lambda t, d: "no")
    assert pm.request(key="bash", title="Run", detail="rm -rf") is Decision.DENY
    pm2 = PermissionManager(auto_approve=False, prompt=lambda t, d: "always")
    assert pm2.request(key="bash", title="Run", detail="ls") is Decision.ALLOW
    # "always" is remembered without prompting again.
    pm2._prompt = lambda t, d: "no"  # would deny if asked
    assert pm2.request(key="bash", title="Run", detail="ls") is Decision.ALLOW


def test_model_resolution_uses_provider_preset():
    mc = ModelConfig(name="g", model="llama-3.3-70b", provider="groq")
    assert mc.resolved_base_url() == "https://api.groq.com/openai/v1"


def test_model_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    mc = ModelConfig(name="x", model="gpt-4o", provider="openai")
    with pytest.raises(ConfigError):
        mc.resolved_api_key()


def test_local_provider_api_key_default():
    mc = ModelConfig(name="l", model="qwen", provider="ollama")
    assert mc.resolved_api_key() == "local"


def test_config_default_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CODED_CONFIG", str(tmp_path / "none.json"))
    cfg = load_config(cwd=str(tmp_path))
    assert cfg.default_model == "gpt-4o"
