"""Tests for the skills system (discovery, frontmatter, and the skill tool)."""

from __future__ import annotations

import json
from pathlib import Path

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.skills import discover_skills, parse_frontmatter, skills_prompt_section
from coded.tools.base import ToolContext
from coded.tools.skill import SkillTool


def _write_skill(root: Path, name: str, description: str, body: str, allowed=None):
    d = root / ".coded" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {description}\n"
    if allowed:
        fm += f"allowed-tools: {allowed}\n"
    fm += "---\n" + body
    (d / "SKILL.md").write_text(fm)
    return d


def test_parse_frontmatter_scalars_and_lists():
    text = "---\nname: x\ndescription: hi there\nallowed-tools: [read, bash]\n---\nBODY\nmore"
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "x"
    assert meta["description"] == "hi there"
    assert meta["allowed-tools"] == ["read", "bash"]
    assert body == "BODY\nmore"


def test_parse_frontmatter_none():
    meta, body = parse_frontmatter("no frontmatter here")
    assert meta == {}
    assert body == "no frontmatter here"


def test_discover_skills(tmp_path):
    _write_skill(tmp_path, "greeter", "Say hi. Use when greeting.", "# Greeter\nSay hello.")
    skills = discover_skills(str(tmp_path))
    assert "greeter" in skills
    s = skills["greeter"]
    assert "greeting" in s.description
    assert s.load_body().startswith("# Greeter")


def test_skills_prompt_section_lists_descriptions(tmp_path):
    _write_skill(tmp_path, "a", "does A", "body A")
    _write_skill(tmp_path, "b", "does B", "body B")
    skills = discover_skills(str(tmp_path))
    section = skills_prompt_section(skills)
    assert "# Available skills" in section
    assert "**a**: does A" in section and "**b**: does B" in section


def test_skill_tool_loads_body(tmp_path):
    _write_skill(tmp_path, "greeter", "Say hi.", "# Greeter\nSay hello nicely.", allowed="bash")
    skills = discover_skills(str(tmp_path))
    tool = SkillTool(skills)
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True), config=None)
    res = tool.run({"name": "greeter"}, ctx)
    assert not res.is_error
    assert "Say hello nicely." in res.content
    assert "Preferred tools: bash" in res.content

    missing = tool.run({"name": "nope"}, ctx)
    assert missing.is_error and "Unknown skill" in missing.content


def test_builtin_role_skills_available(tmp_path):
    """The bundled planner/reviewer/qa/security roles are always discovered."""
    skills = discover_skills(str(tmp_path))
    for role in ("planner", "reviewer", "qa", "security"):
        assert role in skills, role
        assert skills[role].description
        assert skills[role].load_body().strip()


def test_project_skill_overrides_builtin(tmp_path):
    """A project skill with a built-in's name shadows the built-in."""
    _write_skill(tmp_path, "planner", "custom project planner", "# custom body")
    skills = discover_skills(str(tmp_path))
    assert skills["planner"].description == "custom project planner"
    assert "custom body" in skills["planner"].load_body()


def test_agent_registers_skill_tool_and_injects_prompt(tmp_path):
    _write_skill(tmp_path, "greeter", "Say hi.", "# Greeter")
    skills = discover_skills(str(tmp_path))
    cfg = Config()
    model = ModelConfig(name="fake", model="fake", base_url="http://x/v1", api_key="k")
    cfg.add_model(model, make_default=True)
    agent = create_agent(model=model, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(auto_approve=True),
                         skills=skills, stream=False, verbose=False)
    assert agent.registry.get("skill") is not None
    assert "greeter" in agent.session.messages[0]["content"]
