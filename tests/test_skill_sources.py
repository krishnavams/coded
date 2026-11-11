"""Installing skills from the collections people actually publish (skills.sh).

Each test builds a local repository shaped like a real one — a CLAUDE.md
symlink at the top, skills nested under plugins/, multi-line YAML
descriptions — so nothing here touches the network.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coded.plugins import InstallError, install_skills
from coded.skills import discover_skills, parse_frontmatter, skills_prompt_section
from coded.sources import SourceError, parse_source, skills_sh_page

YES = lambda summary: True  # noqa: E731


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("CODED_CONFIG", str(tmp_path / "userhome" / "config.json"))
    p = tmp_path / "project"
    p.mkdir()
    return p


def _skill(path: Path, name: str, desc: str = "Does a thing. Use when asked.") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nBody.\n")
    return path


def _link(target: str, at: Path) -> None:
    try:
        os.symlink(target, at)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra privileges on this system")


# -- symlinks ---------------------------------------------------------------------

def test_a_link_outside_the_skills_does_not_block_the_install(tmp_path, project):
    repo = tmp_path / "repo"
    _skill(repo / "skills" / "alpha", "alpha")
    (repo / "AGENTS.md").write_text("# agents\n")
    _link("AGENTS.md", repo / "CLAUDE.md")          # the usual harmless one
    assert install_skills(str(repo), install_all=True, scope="project",
                          cwd=str(project), confirm=YES) == ["alpha"]
    assert not any(p.is_symlink() for p in (project / ".coded").rglob("*"))


def test_a_link_inside_a_skill_is_still_refused(tmp_path, project):
    repo = tmp_path / "repo"
    skill = _skill(repo / "skills" / "beta", "beta")
    _link(str(Path.home()), skill / "home")         # would copy ~ into the install
    with pytest.raises(SourceError, match="symlink"):
        install_skills(str(repo), install_all=True, scope="project",
                       cwd=str(project), confirm=YES)
    assert not (project / ".coded" / "skills" / "beta").exists()


# -- layouts ------------------------------------------------------------------------

def test_skills_nested_under_plugins_are_found(tmp_path, project):
    repo = tmp_path / "repo"
    _skill(repo / "plugins" / "app" / "skills" / "router", "router")
    _skill(repo / "plugins" / "labs" / "skills" / "migrate", "migrate")
    _skill(repo / "plugins" / "app" / "skills" / "router" / "examples" / "x", "not-a-skill")
    installed = install_skills(str(repo), install_all=True, scope="project",
                               cwd=str(project), confirm=YES)
    assert sorted(installed) == ["migrate", "router"]  # a skill's own sub-folders are not searched


def test_two_skills_with_one_name_are_reported_not_merged(tmp_path, project):
    repo = tmp_path / "repo"
    _skill(repo / "a" / "skills" / "dup", "dup")
    _skill(repo / "b" / "skills" / "dup", "dup")
    with pytest.raises(InstallError, match="both named 'dup'"):
        install_skills(str(repo), install_all=True, scope="project",
                       cwd=str(project), confirm=YES)


# -- frontmatter as real skills write it ------------------------------------------------

def test_multi_line_descriptions_and_nested_metadata():
    text = """---
name: composition
description:
  React composition patterns that scale. Use when refactoring
  components with boolean props.
license: MIT
metadata:
  author: vercel
  version: '1.0.0'
tags:
  - react
  - patterns
---
Body
"""
    meta, body = parse_frontmatter(text)
    assert meta["description"] == ("React composition patterns that scale. Use when "
                                   "refactoring components with boolean props.")
    assert meta["metadata"] == {"author": "vercel", "version": "1.0.0"}
    assert meta["tags"] == ["react", "patterns"]
    assert "author" not in meta and body == "Body"


def test_folded_and_literal_block_scalars():
    folded, _ = parse_frontmatter("---\nname: x\ndescription: >\n  one\n  two\n---\n")
    literal, _ = parse_frontmatter("---\nname: x\ndescription: |\n  one\n  two\n---\n")
    assert folded["description"] == "one two"
    assert literal["description"] == "one\ntwo"


def test_the_simple_frontmatter_still_parses_the_same():
    meta, _ = parse_frontmatter("---\nname: a\ndescription: \"Quoted: yes\"\n"
                                "allowed-tools: [read, grep]\n---\nx")
    assert meta == {"name": "a", "description": "Quoted: yes", "allowed-tools": ["read", "grep"]}


# -- links people paste -------------------------------------------------------------------

def test_skills_sh_and_github_folder_links():
    assert skills_sh_page("https://skills.sh/vercel-labs/agent-skills/react-best") == (
        "vercel-labs/agent-skills", "react-best")
    assert skills_sh_page("skills.sh/anthropics/skills") == ("anthropics/skills", None)
    assert skills_sh_page("vercel-labs/agent-skills") is None

    info = parse_source("https://skills.sh/anthropics/skills/pdf")
    assert info.type == "github" and info.location == "https://github.com/anthropics/skills.git"

    tree = parse_source("https://github.com/anthropics/skills/tree/main/skills/pdf")
    assert (tree.type, tree.ref, tree.subpath) == ("github", "main", "skills/pdf")
    assert tree.location == "https://github.com/anthropics/skills.git"


def test_a_skills_sh_link_installs_just_that_skill(tmp_path, project, monkeypatch):
    repo = tmp_path / "repo"
    _skill(repo / "skills" / "wanted", "wanted")
    _skill(repo / "skills" / "other", "other")
    seen = {}

    def fake_parse(source, ref=None, subpath=None):
        seen["source"] = source
        return real_parse(str(repo), ref, subpath)

    import coded.plugins as plugins
    real_parse = plugins.parse_source
    monkeypatch.setattr(plugins, "parse_source", fake_parse)
    assert install_skills("https://skills.sh/acme/repo/wanted", scope="project",
                          cwd=str(project), confirm=YES) == ["wanted"]
    assert seen["source"] == "acme/repo"


# -- a big collection in the prompt ---------------------------------------------------------

def test_a_large_collection_is_shortened_to_fit_its_budget(tmp_path):
    root = tmp_path / ".coded" / "skills"
    for i in range(80):
        _skill(root / f"skill-{i:02d}", f"skill-{i:02d}",
               "Use when the task involves topic number %d. " % i + "Detail. " * 60)
    skills = {k: v for k, v in discover_skills(str(tmp_path)).items() if k.startswith("skill-")}
    full = skills_prompt_section(skills, str(tmp_path))
    short = skills_prompt_section(skills, str(tmp_path), budget=12_000)
    assert len(full) > 12_000 >= len(short) - 2_000          # names-only tail may overrun a little
    assert "Also installed" in short and "skill-79" in short   # every skill is still named
    assert "Use when the task involves topic number 0." in short


def test_a_small_collection_is_untouched_by_the_budget(tmp_path):
    _skill(tmp_path / ".coded" / "skills" / "one", "one")
    skills = discover_skills(str(tmp_path))
    assert skills_prompt_section(skills, str(tmp_path), budget=24_000) == \
        skills_prompt_section(skills, str(tmp_path))
