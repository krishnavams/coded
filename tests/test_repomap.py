"""The repository map: extraction, ranking, budgeting, and the tool.

The discriminating test is the ranking one — a small, heavily referenced file
must outrank a large one nobody imports, which is the whole reason the map
exists rather than a file listing.
"""

from __future__ import annotations

import json

from coded.repomap import build, build_repo_map, extract_symbols, pagerank, render
from coded.tools.base import ToolContext
from coded.tools.repo_map import RepoMapTool
from coded.permissions import PermissionManager
from coded.config import Config


def _ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True),
                       config=Config(), extra={})


# -- extraction ---------------------------------------------------------------

def test_python_symbols_come_from_the_ast():
    syms = extract_symbols("a.py", (
        "import os\n"
        "MAX_SIZE = 10\n"
        "class Engine:\n"
        "    def start(self):\n"
        "        pass\n"
        "    async def stop(self):\n"
        "        pass\n"
        "def helper():\n"
        "    def inner():\n"  # nested: not a top-level definition
        "        pass\n"
    ))
    rendered = [s.render() for s in syms]
    assert rendered == ["const MAX_SIZE", "class Engine", "method Engine.start",
                        "method Engine.stop", "def helper"]


def test_a_python_file_that_does_not_parse_still_yields_an_outline():
    syms = extract_symbols("broken.py", "class Half:\n    def go(:\n")
    assert [s.name for s in syms] == ["Half"]  # regex fallback, no exception


def test_other_languages_are_covered_conservatively():
    js = [s.name for s in extract_symbols("a.ts", (
        "export class Widget {}\n"
        "export function build(x) {}\n"
        "export const render = (a) => a\n"
        "export interface Props {}\n"
        "const notAFunction = 5\n"))]
    assert js == ["Widget", "build", "render", "Props"]
    assert [s.name for s in extract_symbols("a.go", "func Serve() {}\ntype Server struct{}\n")] \
        == ["Serve", "Server"]
    assert [s.name for s in extract_symbols("a.rs",
                                            "pub fn run() {}\npub struct App;\n")] == ["run", "App"]
    assert extract_symbols("notes.txt", "def nope(): pass") == []


def test_binary_and_minified_files_are_skipped(tmp_path):
    (tmp_path / "ok.py").write_text("class Real: pass\n")
    (tmp_path / "bundle.js").write_text("var a=1;" + "x" * 5000 + "\n")
    (tmp_path / "blob.py").write_bytes(b"\x00\x01\x02 class Fake: pass")
    result = build(str(tmp_path), ["ok.py", "bundle.js", "blob.py"])
    assert result.files == ["ok.py"]


# -- ranking ------------------------------------------------------------------

def _fixture_repo(tmp_path):
    """A small repo where importance and size disagree."""
    (tmp_path / "core.py").write_text(
        "class Ledger:\n    def post(self): pass\n\ndef audit(x): return x\n")
    # Three files depend on core; none depends on the big one.
    for i in range(3):
        (tmp_path / f"user{i}.py").write_text(
            f"from core import Ledger, audit\n\n"
            f"def handler{i}():\n    return Ledger(), audit(1)\n")
    (tmp_path / "generated.py").write_text(
        "\n".join(f"def spare_{i}():\n    return {i}\n" for i in range(300)))
    return ["core.py", "generated.py"] + [f"user{i}.py" for i in range(3)]


def test_a_small_referenced_file_outranks_a_large_ignored_one(tmp_path):
    files = _fixture_repo(tmp_path)
    result = build(str(tmp_path), files)
    assert result.files[0] == "core.py"
    assert result.rank["core.py"] > result.rank["generated.py"]
    assert result.files.index("generated.py") > result.files.index("core.py")


def test_a_tight_budget_keeps_the_top_of_the_ranking(tmp_path):
    files = _fixture_repo(tmp_path)
    result = build(str(tmp_path), files)
    text = render(result, 260)
    assert "core.py" in text
    assert "generated.py" not in text
    assert "more files omitted" in text
    assert len(text) < 600


def test_methods_and_ubiquitous_names_do_not_drive_the_ranking(tmp_path):
    """`run` and `save` are defined everywhere; a file full of them is not the
    centre of the codebase."""
    (tmp_path / "common.py").write_text(
        "class A:\n    def run(self): pass\n    def save(self): pass\n"
        "    def load(self): pass\n    def text(self): pass\n")
    (tmp_path / "hub.py").write_text("class Dispatcher:\n    pass\n")
    for i in range(3):
        (tmp_path / f"m{i}.py").write_text(
            "from hub import Dispatcher\n"
            "def run(): pass\ndef save(): pass\ndef load(): pass\ndef text(): pass\n"
            "x = Dispatcher()\n")
    result = build(str(tmp_path), ["common.py", "hub.py"] + [f"m{i}.py" for i in range(3)])
    assert result.files[0] == "hub.py"


def test_focus_pulls_the_matching_files_to_the_front(tmp_path):
    files = _fixture_repo(tmp_path)
    text = build_repo_map(str(tmp_path), 4000, focus="handler2")
    first = [ln for ln in text.splitlines() if ln.endswith(".py")][0]
    assert first == "user2.py"


def test_pagerank_is_stable_and_sums_to_one():
    edges = {"a": {"b": 1.0}, "b": {"c": 1.0}, "c": {"b": 1.0}}
    rank = pagerank(edges, ["a", "b", "c"])
    assert abs(sum(rank.values()) - 1.0) < 1e-6
    assert rank["b"] > rank["a"]  # two inbound edges vs none
    assert pagerank({}, []) == {}


# -- the tool -----------------------------------------------------------------

def test_the_tool_maps_a_subtree_and_refuses_a_missing_one(tmp_path):
    _fixture_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "inner.py").write_text("class Inner: pass\n")

    tool = RepoMapTool()
    out = tool.run({"path": "pkg"}, _ctx(tmp_path))
    assert not out.is_error and "inner.py" in out.content and "core.py" not in out.content

    missing = tool.run({"path": "nope"}, _ctx(tmp_path))
    assert missing.is_error and "No files under" in missing.content


def test_the_tool_says_so_when_there_is_nothing_to_map(tmp_path):
    (tmp_path / "data.csv").write_text("a,b\n1,2\n")
    out = RepoMapTool().run({}, _ctx(tmp_path))
    assert out.is_error and "No definitions" in out.content


def test_the_tool_is_parallel_safe_and_needs_no_permission():
    tool = RepoMapTool()
    assert tool.parallel_safe is True
    assert tool.requires_permission is False
    assert json.dumps(tool.parameters)  # a valid schema


def test_the_names_view_is_terser(tmp_path):
    _fixture_repo(tmp_path)
    ctx = _ctx(tmp_path)
    kinds = RepoMapTool().run({}, ctx).content
    names = RepoMapTool().run({"detail": "names"}, ctx).content
    assert "class Ledger" in kinds
    assert "class Ledger" not in names and "Ledger" in names
