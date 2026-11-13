"""Dependency map: imports, dependents, impact (with the tests to run), cycles."""

from __future__ import annotations

from pathlib import Path

from coded import depgraph
from coded.config import Config
from coded.permissions import PermissionManager
from coded.tools.base import ToolContext
from coded.tools.repo_map import RepoMapTool


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _project(tmp_path):
    _write(tmp_path, {
        "src/shop/__init__.py": "",
        "src/shop/models.py": "class Order: pass\n",
        "src/shop/pricing.py": "from .models import Order\nimport json\n",
        "src/shop/api.py": "from shop import pricing\nfrom shop.models import Order\n"
                           "def handler():\n    from . import late\n",
        "src/shop/late.py": "",
        "tests/test_pricing.py": "from shop.pricing import *\n",
        "tests/test_api.py": "import shop.api\n",
        "tests/test_other.py": "import os\n",
        "web/util.ts": "export const x = 1\n",
        "web/app.ts": "import { x } from './util'\nimport React from 'react'\n"
                      "const y = require('./lib/index.js')\n",
        "web/lib/index.js": "module.exports = {}\n",
        "web/app.test.ts": "import './app'\n",
    })
    return depgraph.build(str(tmp_path), [str(p.relative_to(tmp_path)).replace("\\", "/")
                                          for p in tmp_path.rglob("*") if p.is_file()])


def test_python_and_js_imports_resolve_to_files(tmp_path):
    g = _project(tmp_path)
    assert g.imports["src/shop/pricing.py"] == {"src/shop/models.py"}
    assert g.external["src/shop/pricing.py"] == {"json"}
    assert g.imports["src/shop/api.py"] == {"src/shop/pricing.py", "src/shop/models.py",
                                            "src/shop/late.py"}
    assert g.lazy["src/shop/api.py"] == {"src/shop/late.py"}  # imported inside a function
    assert g.imports["web/app.ts"] == {"web/util.ts", "web/lib/index.js"}
    assert g.external["web/app.ts"] == {"react"}


def test_impact_lists_everything_affected_and_the_tests_to_run(tmp_path):
    g = _project(tmp_path)
    hits = depgraph.impact(g, ["src/shop/models.py"])
    assert hits["src/shop/pricing.py"] == 1 and hits["src/shop/api.py"] == 1
    assert hits["tests/test_pricing.py"] == 2
    assert "tests/test_other.py" not in hits
    assert depgraph.affected_tests(g, ["src/shop/models.py"]) == [
        "tests/test_api.py", "tests/test_pricing.py"]
    assert depgraph.affected_tests(g, ["web/util.ts"]) == ["web/app.test.ts"]
    text = depgraph.render_impact(g, ["src/shop/models.py"], 4000)
    assert "directly (imports it)" in text and "Tests to run (2)" in text


def test_cycles_ignore_imports_made_inside_functions(tmp_path):
    _write(tmp_path, {"a.py": "import b\n", "b.py": "import c\n", "c.py": "import a\n",
                      "d.py": "def f():\n    import e\n", "e.py": "import d\n"})
    g = depgraph.build(str(tmp_path), ["a.py", "b.py", "c.py", "d.py", "e.py"])
    assert depgraph.cycles(g) == [["a.py", "b.py", "c.py"]]


def test_codedignore_and_bounds(tmp_path):
    _write(tmp_path, {"a.py": "import secret\n", "secret.py": "", ".codedignore": "secret.py\n"})
    from coded.ignore import rules_for

    g = depgraph.build(str(tmp_path), ["a.py", "secret.py"],
                       ignored=rules_for(str(tmp_path)).ignored)
    assert "secret.py" not in g.files
    g = depgraph.build(str(tmp_path), ["a.py", "secret.py"], max_files=1)
    assert g.truncated


def test_repo_map_tool_operations(tmp_path):
    _project(tmp_path)
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=Config())
    tool = RepoMapTool()
    assert "is imported by 2 file(s)" in tool.run(
        {"operation": "dependents", "path": "src/shop/pricing.py"}, ctx).content
    out = tool.run({"operation": "impact", "path": "src/shop/models.py, web/util.ts"}, ctx)
    assert "web/app.test.ts" in out.content and "tests/test_api.py" in out.content
    assert "outside the repo: react" in tool.run(
        {"operation": "imports", "path": "web/app.ts"}, ctx).content
    assert tool.run({"operation": "cycles"}, ctx).content.startswith("No import cycles")
    assert tool.run({"operation": "impact"}, ctx).is_error
    assert tool.run({"operation": "impact", "path": "nope.py"}, ctx).is_error
    assert tool.run({"operation": "bogus"}, ctx).is_error
    # No operation: the ranked outline, as before.
    assert "class Order" in tool.run({}, ctx).content
