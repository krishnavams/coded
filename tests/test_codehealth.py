"""Refactor finder: code-health scan, repo_map health, the refactor skill and /refactor."""

from __future__ import annotations

from coded import codehealth
from coded.config import Config
from coded.permissions import PermissionManager
from coded.tools.base import ToolContext
from coded.tools.repo_map import RepoMapTool


def _kinds(findings):
    return {(f.name, f.kind) for f in findings}


def test_function_metrics():
    body = "\n".join(f"    x{i} = {i}" for i in range(70))
    branchy = "\n".join(f"    if a == {i}:\n        return {i}" for i in range(14))
    nested = ("def deep(a):\n    if a:\n        for b in a:\n            while b:\n"
              "                with b:\n                    if b:\n                        pass\n")
    chain = "def chain(a):\n    if a == 1:\n        pass\n" + "".join(
        f"    elif a == {i}:\n        pass\n" for i in range(2, 20))
    src = (f"def long():\n{body}\n\ndef branchy(a):\n{branchy}\n\n{nested}\n{chain}\n"
           "class K:\n    def method(self, a, b, c, d, e, f):\n        pass\n"
           "    def wide(self, a, b, c, d, e, f, g):\n        pass\n")
    found = _kinds(codehealth.python_findings("m.py", src))
    assert ("long", "long-function") in found
    assert ("branchy", "complex") in found
    assert ("deep", "deep-nesting") in found
    assert ("chain", "deep-nesting") not in found  # an elif chain is not nesting
    assert ("K.wide", "many-params") in found and ("K.method", "many-params") not in found


def test_duplicates_across_files_and_ignored_files(tmp_path):
    block = "\n".join(f"    total_value = compute_something({i}, other_arg)" for i in range(8))
    (tmp_path / "a.py").write_text(f"def a():\n{block}\n")
    (tmp_path / "b.py").write_text(f"def b():\n{block}\n")
    (tmp_path / "c.py").write_text(f"def c():\n{block}\n")
    findings, _ = codehealth.scan(str(tmp_path), ["a.py", "b.py", "c.py"])
    dups = [f for f in findings if f.kind == "duplicate"]
    assert dups and {"b.py", "c.py"} <= {d.detail.split("also at ")[1].split(":")[0] for d in dups}
    from coded.ignore import rules_for

    (tmp_path / ".codedignore").write_text("b.py\nc.py\n")
    findings, _ = codehealth.scan(str(tmp_path), ["a.py", "b.py", "c.py"],
                                  ignored=rules_for(str(tmp_path)).ignored)
    assert not [f for f in findings if f.kind == "duplicate"]


def test_ranking_prefers_files_others_depend_on(tmp_path):
    big = "def f(a):\n" + "".join(f"    if a == {i}:\n        return {i}\n" for i in range(14))
    (tmp_path / "core.py").write_text(big)
    (tmp_path / "leaf.py").write_text(big)
    for i in range(4):
        (tmp_path / f"user{i}.py").write_text("import core\n")
    findings, _ = codehealth.scan(str(tmp_path), ["core.py", "leaf.py"] +
                                  [f"user{i}.py" for i in range(4)])
    assert [f.path for f in findings if f.kind == "complex"][:2] == ["core.py", "leaf.py"]


def test_repo_map_health_and_the_refactor_skill(tmp_path):
    (tmp_path / "app.py").write_text("def f(a, b, c, d, e, f2, g):\n    pass\n")
    (tmp_path / "ok.py").write_text("def g():\n    return 1\n")
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=Config())
    out = RepoMapTool().run({"operation": "health"}, ctx).content
    assert "app.py:1  many-params  f" in out
    clean = RepoMapTool().run({"operation": "health", "path": "ok.py"}, ctx).content
    assert clean.startswith("No refactoring hot spots")
    from coded.skills import discover_skills

    skill = discover_skills(str(tmp_path))["refactor"]
    body = skill.load_body()
    assert "health" in body and "impact" in body


def test_refactor_command_starts_a_turn(tmp_path, headless):
    from coded.agent import create_agent
    from coded.config import ModelConfig
    from coded.repl import Repl

    cfg = Config()
    m = ModelConfig(name="m", model="m", base_url="http://x/v1", api_key="k")
    cfg.add_model(m, make_default=True)
    agent = create_agent(model=m, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(), stream=False, verbose=False)
    repl = Repl(agent, cfg)
    ran = []
    agent.run = lambda text, **kw: ran.append(text) or ""
    repl._handle_slash("/refactor src/billing")
    assert "refactor skill" in ran[0] and "src/billing" in ran[0]
