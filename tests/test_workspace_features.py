"""--add-dir, .codedignore, notebooks and diagnostics after edits."""

from __future__ import annotations

import json

import pytest

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.ignore import IgnoreRules
from coded.permissions import PermissionManager
from coded.tools.base import ToolContext


def _agent(tmp_path, base_url="http://x/v1", **cfg_kw):
    cfg = Config(**cfg_kw)
    m = ModelConfig(name="m", model="m", base_url=base_url, api_key="k")
    cfg.add_model(m, make_default=True)
    return create_agent(model=m, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(auto_approve=True), stream=False,
                        verbose=False)


class _TC:
    def __init__(self, name, args):
        self.id, self.name, self.arguments = "t1", name, json.dumps(args)


# -- .codedignore -------------------------------------------------------------------

def test_ignore_patterns_follow_gitignore(tmp_path):
    rules = IgnoreRules(str(tmp_path), [
        "# secrets", "*.pem", "secrets/", "/build", "docs/**/private.md", "!keep.pem", ""])
    assert rules.ignored_rel("a/b/server.pem")
    assert not rules.ignored_rel("keep.pem")
    assert rules.ignored_rel("secrets", is_dir=True) and rules.ignored_rel("secrets/x.txt")
    assert not rules.ignored_rel("secrets")          # a file named like the folder
    assert rules.ignored_rel("build/out.js") and not rules.ignored_rel("src/build/out.js")
    assert rules.ignored_rel("docs/a/b/private.md") and rules.ignored_rel("docs/private.md")
    assert not rules.ignored_rel("src/app.py")
    assert not rules.ignored(tmp_path.parent / "elsewhere.pem")  # outside the project


def _ignored_project(tmp_path):
    (tmp_path / ".codedignore").write_text("secrets/\n*.key\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "prod.env").write_text("TOKEN=abc\n")
    (tmp_path / "api.key").write_text("k\n")
    (tmp_path / "app.py").write_text("TOKEN = 'x'\n")
    return tmp_path


def test_file_tools_refuse_ignored_paths(tmp_path):
    agent = _agent(_ignored_project(tmp_path))
    for name, args in (("read", {"path": "secrets/prod.env"}),
                       ("edit", {"path": "api.key", "old_string": "k", "new_string": "j"}),
                       ("apply_patch", {"patch": "*** Begin Patch\n*** Delete File: api.key\n"
                                                 "*** End Patch"})):
        content, is_error, _ = agent._run_tool(_TC(name, args), display=False)
        assert is_error and ".codedignore" in content, name
    assert (tmp_path / "api.key").read_text() == "k\n"
    content, is_error, _ = agent._run_tool(_TC("read", {"path": "app.py"}), display=False)
    assert not is_error


def test_search_listing_and_mentions_skip_ignored_paths(tmp_path):
    from coded.mentions import MentionCompleter, expand_mentions
    from coded.tools.files import LsTool
    from coded.tools.search import GlobTool, GrepTool

    _ignored_project(tmp_path)
    ctx = ToolContext(cwd=str(tmp_path), permissions=None, config=Config())
    grep = GrepTool().run({"pattern": "TOKEN"}, ctx).content
    assert "app.py" in grep and "prod.env" not in grep
    assert "api.key" not in GlobTool().run({"pattern": "*"}, ctx).content
    listing = LsTool().run({}, ctx).content
    assert "app.py" in listing and "secrets" not in listing and "api.key" not in listing

    exp = expand_mentions("look at @api.key", str(tmp_path))
    assert "excluded by .codedignore" in exp.text and "k\n" not in exp.text.split("excluded")[1]
    from prompt_toolkit.document import Document

    got = [c.text for c in MentionCompleter(str(tmp_path)).get_completions(Document("@"), None)]
    assert "@app.py" in got and not any("secrets" in g or "api.key" in g for g in got)


# -- --add-dir ------------------------------------------------------------------------

def test_add_dir_reaches_the_prompt_sandbox_and_cli(tmp_path):
    from coded.cli import _apply_overrides, build_parser
    from coded.config import ConfigError
    from coded.sandbox import sandbox_state

    other = tmp_path / "shared"
    other.mkdir()
    cfg = Config()
    _apply_overrides(cfg, build_parser().parse_args(["--cwd", str(tmp_path),
                                                     "--add-dir", "shared"]))
    assert cfg.add_dirs == [str(other)]
    with pytest.raises(ConfigError):
        _apply_overrides(Config(), build_parser().parse_args(["--add-dir", str(tmp_path / "nope")]))

    agent = _agent(tmp_path, add_dirs=[str(other)])
    assert f"Also yours to work in (use absolute paths): {other}" in agent.session.messages[0]["content"]
    plain = _agent(tmp_path)
    assert "Also yours" not in plain.session.messages[0]["content"]
    settings, _ = sandbox_state(Config(add_dirs=[str(other)], sandbox={"enabled": True}))
    assert str(other) in settings.writable


def test_add_dir_command(tmp_path, headless):
    from coded.repl import Repl

    (tmp_path / "lib").mkdir()
    agent = _agent(tmp_path)
    repl = Repl(agent, agent.config)
    repl._handle_slash("/add-dir lib")
    assert agent.config.add_dirs == [str(tmp_path / "lib")]
    assert str(tmp_path / "lib") in agent.session.messages[0]["content"]


# -- notebooks -------------------------------------------------------------------------

def _notebook(tmp_path):
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"cell_type": "markdown", "id": "intro", "metadata": {}, "source": ["# Title\n", "Text"]},
        {"cell_type": "code", "id": "load", "metadata": {}, "execution_count": 3,
         "source": ["x = 1\n", "print(x)"],
         "outputs": [{"output_type": "stream", "name": "stdout", "text": ["1\n"]},
                     {"output_type": "display_data", "data": {"image/png": "AAA"}}]},
    ]}
    path = tmp_path / "work.ipynb"
    path.write_text(json.dumps(nb, indent=1))
    return path


def test_read_shows_notebook_cells_and_outputs(tmp_path):
    from coded.tools.files import ReadTool

    _notebook(tmp_path)
    ctx = ToolContext(cwd=str(tmp_path), permissions=None, config=Config())
    out = ReadTool().run({"path": "work.ipynb"}, ctx).content
    assert "[IPYNB: 2 cells]" in out and "cell 2 [code] id=load" in out
    assert "print(x)" in out and "----- output -----" in out and "<image/png output>" in out
    only = ReadTool().run({"path": "work.ipynb", "pages": "1"}, ctx).content
    assert "# Title" in only and "print(x)" not in only


def test_notebook_edit_replaces_inserts_and_deletes_cells(tmp_path):
    from coded.tools.notebook import NotebookEditTool

    path = _notebook(tmp_path)
    ctx = ToolContext(cwd=str(tmp_path), permissions=None, config=Config())
    tool = NotebookEditTool()
    r = tool.run({"path": "work.ipynb", "cell_id": "load", "new_source": "y = 2\nprint(y)"}, ctx)
    assert not r.is_error
    nb = json.loads(path.read_text())
    code = nb["cells"][1]
    assert code["source"] == ["y = 2\n", "print(y)"]
    assert code["outputs"] == [] and code["execution_count"] is None
    assert path.read_text().startswith('{\n "')  # the notebook's own indentation kept

    tool.run({"path": "work.ipynb", "cell_number": 3, "edit_mode": "insert",
              "new_source": "## Notes", "cell_type": "markdown"}, ctx)
    nb = json.loads(path.read_text())
    assert [c["cell_type"] for c in nb["cells"]] == ["markdown", "code", "markdown"]
    assert nb["cells"][2].get("id") and "outputs" not in nb["cells"][2]

    tool.run({"path": "work.ipynb", "cell_number": 1, "edit_mode": "delete"}, ctx)
    assert len(json.loads(path.read_text())["cells"]) == 2
    bad = tool.run({"path": "work.ipynb", "cell_number": 9, "new_source": "x"}, ctx)
    assert bad.is_error and "has 2" in bad.content
    assert tool.run({"path": "a.py", "cell_number": 1, "new_source": "x"}, ctx).is_error


def test_claude_plugins_map_notebook_edit():
    from coded.plugins import TOOL_NAME_MAP
    from coded.tools import build_registry

    assert TOOL_NAME_MAP["NotebookEdit"] == "notebook_edit"
    assert build_registry().get("notebook_edit") is not None


# -- diagnostics ---------------------------------------------------------------------

def test_diagnostics_settings_and_matching():
    from coded.diagnostics import command_for, settings

    assert settings(Config()) == ({}, 20.0)
    cmds, timeout = settings(Config(diagnostics={"commands": {"*.py": "mypy {file}"},
                                                 "timeout": 5}))
    assert cmds == {"*.py": "mypy {file}"} and timeout == 5
    builtin, _ = settings(Config(diagnostics=True), which=lambda n: None)
    assert "py_compile" in builtin["*.py"] and "*.sh" not in builtin
    both, _ = settings(Config(diagnostics={"builtin": True, "commands": {"*.py": "mine {file}"}}))
    assert command_for("src/a.py", both) == "mine {file}"
    assert command_for("README.md", both) is None


def test_a_broken_edit_comes_back_with_its_diagnostics(tmp_path):
    agent = _agent(tmp_path, diagnostics=True)
    (tmp_path / "ok.py").write_text("x = 1\n")
    content, is_error, _ = agent._run_tool(
        _TC("write", {"path": "bad.py", "content": "def f(:\n    pass\n"}), display=False)
    assert not is_error and "<diagnostics>" in content and "bad.py" in content
    content, _, _ = agent._run_tool(
        _TC("edit", {"path": "ok.py", "old_string": "x = 1", "new_string": "x = 2"}),
        display=False)
    assert "<diagnostics>" not in content
    quiet = _agent(tmp_path)  # off unless configured
    content, _, _ = quiet._run_tool(
        _TC("write", {"path": "bad2.py", "content": "def f(:\n"}), display=False)
    assert "<diagnostics>" not in content
