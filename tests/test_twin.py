"""Project twin: a per-file cache that gives the same answers, stays out of the
repository, survives corruption, and reports what changed."""

from __future__ import annotations

import io
import os
import time

from coded import codehealth, depgraph, twin


def _project(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "core.py").write_text(
        "def f(a):\n" + "".join(f"    if a == {i}:\n        return {i}\n" for i in range(14)))
    (tmp_path / "pkg" / "api.py").write_text("from pkg import core\nimport json\n")
    (tmp_path / "web.ts").write_text("import { x } from './util'\n")
    (tmp_path / "util.ts").write_text("export const x = 1\n")
    return ["pkg/__init__.py", "pkg/core.py", "pkg/api.py", "web.ts", "util.ts"]


def _same(a, b):
    return (a.imports, a.external, a.lazy) == (b.imports, b.external, b.lazy)


def test_answers_are_identical_with_and_without_the_twin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    files = _project(tmp_path)
    plain_graph = depgraph.build(str(tmp_path), files)
    plain_health, _ = codehealth.scan(str(tmp_path), files)
    t = twin.Twin(str(tmp_path))
    first = depgraph.build(str(tmp_path), files, twin=t)
    health1, _ = codehealth.scan(str(tmp_path), files, twin=t)
    t.save()
    t2 = twin.Twin(str(tmp_path))  # from disk
    second = depgraph.build(str(tmp_path), files, twin=t2)
    health2, _ = codehealth.scan(str(tmp_path), files, twin=t2)
    assert _same(plain_graph, first) and _same(plain_graph, second)
    key = [(f.path, f.line, f.kind, round(f.score, 6)) for f in plain_health]
    assert key == [(f.path, f.line, f.kind, round(f.score, 6)) for f in health1]
    assert key == [(f.path, f.line, f.kind, round(f.score, 6)) for f in health2]
    assert t2.misses == 0 and t2.hits > 0  # nothing re-read
    # A changed file is re-read; the rest still come from the twin.
    time.sleep(0.01)
    (tmp_path / "pkg" / "api.py").write_text("from pkg import core\nimport os\n")
    t3 = twin.Twin(str(tmp_path))
    g3 = depgraph.build(str(tmp_path), files, twin=t3)
    assert g3.external["pkg/api.py"] == {"os"} and t3.misses == 1


def test_cache_lives_outside_the_repo_and_tolerates_corruption(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    proj = tmp_path / "proj"
    proj.mkdir()
    files = _project(proj)
    t = twin.Twin(str(proj))
    depgraph.build(str(proj), files, twin=t)
    t.save()
    assert str(t.path).startswith(str(tmp_path / "cache")) and not (proj / ".coded").exists()
    t.path.write_text("{not json")
    t2 = twin.Twin(str(proj))
    assert t2.files == {} and depgraph.build(str(proj), files, twin=t2).imports


def test_twin_view_reports_changes_since_last_look(tmp_path, monkeypatch):
    from rich.console import Console

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    proj = tmp_path / "proj"
    proj.mkdir()
    _project(proj)
    facts = twin.refresh(str(proj))
    assert facts["first"] and facts["languages"] == {"Python": 3, "TypeScript": 2}
    assert ("pkg/core.py", 1) in facts["central"] and facts["hot_spot_count"] >= 1
    (proj / "new.py").write_text("x = 1\n")
    os.remove(proj / "util.ts")
    time.sleep(0.01)
    (proj / "pkg" / "api.py").write_text("from pkg import core\n")
    facts = twin.refresh(str(proj))
    assert (facts["added"], facts["removed"], facts["changed"]) == (
        ["new.py"], ["util.ts"], ["pkg/api.py"])
    con = Console(width=110, record=True, file=io.StringIO(), color_system=None)
    con.print(twin.render(facts))
    out = con.export_text()
    assert "Project twin" in out and "added 1: new.py" in out and "removed 1: util.ts" in out
