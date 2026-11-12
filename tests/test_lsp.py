"""The lsp tool: a real stdio JSON-RPC session with tests/fake_lsp.py (offline)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from coded import lsp
from coded.config import Config
from coded.permissions import PermissionManager
from coded.tools import build_registry
from coded.tools.base import ToolContext
from coded.tools.lsp import LspTool

FAKE = str(Path(__file__).with_name("fake_lsp.py"))

SOURCE = """class Box:
    def open(self):
        tmp = 1  # TODO tidy
        return tmp

def helper():
    return Box().open()
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    yield tmp_path
    lsp.stop_all()


def _tool(**langs):
    settings = {"python": [sys.executable, FAKE], "timeout": 10, **langs}
    return LspTool(settings)


def _run(tool, root, **args):
    ctx = ToolContext(cwd=str(root), permissions=PermissionManager(), config=Config(), extra={})
    return tool.run(args, ctx)


def test_position_operations(project):
    tool = _tool()
    got = _run(tool, project, operation="definition", path="app.py", line=7, symbol="open")
    assert got.content == ("1 definition of open:\n"
                           "app.py:2:5  def open(self):"), got.content
    refs = _run(tool, project, operation="references", path="app.py", line=7, symbol="Box").content
    assert refs.splitlines()[0] == "2 references of Box:"
    assert "app.py:1:7  class Box:" in refs and "app.py:7:12  return Box().open()" in refs
    hover = _run(tool, project, operation="hover", path="app.py", line=7, symbol="open").content
    assert hover.startswith("**open** at character 17;")


def test_symbol_must_be_on_the_line_and_columns_are_utf16(project):
    tool = _tool()
    bad = _run(tool, project, operation="hover", path="app.py", line=1, symbol="helper")
    assert bad.is_error and "class Box:" in bad.content
    assert _run(tool, project, operation="hover", path="app.py", line=1).is_error
    (project / "emoji.py").write_text("x = '😀😀'; value = 1\n", encoding="utf-8")
    hover = _run(tool, project, operation="hover", path="emoji.py", line=1, symbol="value")
    # Python index 10, but each emoji is two UTF-16 units; the server found the word.
    assert hover.content.startswith("**value** at character 12;"), hover.content


def test_edits_on_disk_are_resent(project):
    tool = _tool()
    first = _run(tool, project, operation="hover", path="app.py", line=1, symbol="Box").content
    assert "7 lines" in first
    (project / "app.py").write_text(SOURCE + "\nextra = 1\n", encoding="utf-8")
    import os
    os.utime(project / "app.py", (1, 1))  # a different mtime even on coarse clocks
    again = _run(tool, project, operation="hover", path="app.py", line=1, symbol="Box").content
    assert "9 lines" in again


def test_outline_workspace_search_and_diagnostics(project):
    tool = _tool()
    outline = _run(tool, project, operation="symbols", path="app.py").content
    assert outline == ("Symbols in app.py:\nclass Box  line 1\n"
                       "  method open  (self)  line 2"), outline   # the local `tmp` is dropped
    found = _run(tool, project, operation="workspace_symbols", query="helper").content
    assert found == "Symbols matching 'helper':\nfunction helper (in mod)  app.py:1"
    diags = _run(tool, project, operation="diagnostics", path="app.py").content
    assert diags == ("1 problem(s) in app.py:\n"
                     "app.py:3:1 warning: a TODO is left [fake]")


def test_missing_and_unknown_servers_explain_what_to_do(project):
    (project / "main.go").write_text("package main\n")
    tool = _tool(go={"command": ["no-such-gopls"]})
    got = _run(tool, project, operation="symbols", path="main.go")
    assert got.is_error and "no go language server found" in got.content
    (project / "notes.xyz").write_text("x\n")
    got = _run(tool, project, operation="symbols", path="notes.xyz")
    assert got.is_error and "no language server is set up for .xyz" in got.content
    got = _run(_tool(python=False), project, operation="symbols", path="app.py")
    assert got.is_error and "no language server is set up for .py" in got.content
    assert _run(tool, project, operation="symbols", path="gone.py").is_error
    assert _run(tool, project, operation="frobnicate").is_error


def test_a_crashed_server_is_an_error_not_a_hang(project):
    (project / "boom.py").write_text("CRASH = 1\n")
    tool = _tool()
    got = _run(tool, project, operation="hover", path="boom.py", line=1, symbol="CRASH")
    assert got.is_error and "stopped" in got.content


def test_settings_table():
    specs, timeout = lsp.server_specs({"python": "pylsp --verbose",
                                       "zig": {"command": ["zls"], "extensions": ["zig"]},
                                       "rust": False, "timeout": 5})
    by = {s.language: s for s in specs}
    assert by["python"].commands == [["pylsp", "--verbose"]] and ".py" in by["python"].extensions
    assert by["zig"].extensions == (".zig",) and "rust" not in by and timeout == 5
    found = lsp.find_command(by["python"], which=lambda n: "/bin/" + n)
    assert found == ["/bin/pylsp", "--verbose"]
    default = {s.language: s for s in lsp.server_specs(True)[0]}["python"]
    assert lsp.find_command(default, which=lambda n: "/x/pylsp" if n == "pylsp" else None) == ["/x/pylsp"]


def test_uri_round_trip(tmp_path):
    path = tmp_path / "a dir" / "ü.py"
    assert lsp.uri_to_path(lsp.path_to_uri(str(path))) == str(path.resolve())
    assert lsp.utf16_col("😀a", 1) == 2 and lsp.py_col("😀a", 2) == 1


def test_registered_only_when_enabled(tmp_path):
    from coded.agent import create_agent
    from coded.config import ModelConfig

    def agent(**cfg_kw):
        cfg = Config(**cfg_kw)
        m = ModelConfig(name="m", model="m", base_url="http://x/v1", api_key="k")
        cfg.add_model(m, make_default=True)
        return create_agent(model=m, config=cfg, cwd=str(tmp_path),
                            permissions=PermissionManager(), stream=False, verbose=False)

    assert "lsp" not in agent().registry.names()
    assert "lsp" in agent(lsp=True).registry.names()
    assert "lsp" not in build_registry().names()


def test_tool_title():
    from coded import ui

    assert ui.tool_title("lsp", {"operation": "references", "symbol": "Box",
                                 "path": "/p/app.py", "line": 7}, "/p") == \
        ("LSP", "references · Box in app.py:7")
    assert ui.tool_title("lsp", {"operation": "workspace_symbols", "query": "helper"}) == \
        ("LSP", "workspace_symbols · helper")


@pytest.mark.skipif(not (shutil.which("pylsp") or shutil.which("pyright-langserver")),
                    reason="no real Python language server installed")
def test_against_a_real_python_server(project):
    tool = LspTool(True)
    got = _run(tool, project, operation="definition", path="app.py", line=7, symbol="open")
    assert "app.py:2:" in got.content, got.content


def test_doctor_lists_language_servers(tmp_path):
    import json

    from coded.doctor import run_checks

    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"lsp": True}))
    which = lambda n: f"/usr/bin/{n}" if n in ("gopls", "pylsp") else None  # noqa: E731
    line = next(c for c in run_checks(str(tmp_path), config_path=str(cfg), which=which)
                if c.area == "code intelligence")
    assert "python: pylsp" in line.detail and "go: gopls" in line.detail
    assert "not found: " in line.detail and "rust" in line.detail
    cfg.write_text("{}")
    line = next(c for c in run_checks(str(tmp_path), config_path=str(cfg), which=which)
                if c.area == "code intelligence")
    assert line.detail.startswith("off")


def test_ignored_files_show_a_location_but_not_their_text(project):
    (project / "secret.py").write_text("API_KEY = 'hunter2'  # Box\n", encoding="utf-8")
    (project / ".codedignore").write_text("secret.py\n")
    from coded.tools.lsp import _format_locations

    shown = _format_locations([(str(project / "secret.py"), 0, 0),
                               (str(project / "app.py"), 0, 6)], str(project))
    assert "hunter2" not in shown and "secret.py:1:1" in shown
    assert "app.py:1:7  class Box:" in shown


def test_a_second_edit_in_the_same_tick_is_resent(project):
    import os

    tool = _tool()
    path = project / "app.py"
    assert "7 lines" in _run(tool, project, operation="hover", path="app.py", line=1,
                             symbol="Box").content
    stamp = os.stat(path)
    path.write_text(SOURCE + "\nmore = 2\n", encoding="utf-8")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))   # same mtime, new size
    assert "9 lines" in _run(tool, project, operation="hover", path="app.py", line=1,
                             symbol="Box").content
