"""Editable prompt files: loading, overrides, and where each one is used."""

from __future__ import annotations

import pytest

from coded.prompt_files import PROMPT_NAMES, builtin_prompts_dir, load_prompt, resolve_prompt
from coded.prompts import build_subagent_prompt, build_system_prompt
from coded.skills import discover_skills, skills_prompt_section


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    user = tmp_path / "user"
    monkeypatch.setenv("CODED_CONFIG", str(user / "config.json"))
    project = tmp_path / "project"
    project.mkdir()
    return user / "prompts", project / ".coded" / "prompts", project


def _first_line(name):
    return (builtin_prompts_dir() / f"{name}.md").read_text().strip().splitlines()[0]


def test_every_builtin_prompt_exists_and_lands_where_expected(dirs):
    _, _, project = dirs
    for name in PROMPT_NAMES:
        assert load_prompt(name), name
    main = build_system_prompt(str(project))
    for name in ("system", "coder", "tools", "security"):
        assert _first_line(name) in main
    assert main.startswith("You are coded")
    assert "# Environment" in main and str(project) in main
    sub = build_subagent_prompt(str(project))
    assert sub.startswith("You are a focused sub-agent")
    assert _first_line("coder") in sub and _first_line("security") in sub
    assert _first_line("system") not in sub
    skills = discover_skills(str(project))
    assert skills["planner"].load_body() == load_prompt("planner")
    assert skills["reviewer"].load_body() == load_prompt("reviewer")
    assert skills["debugger"].load_body() == load_prompt("debugger")
    assert _first_line("skills") in skills_prompt_section(skills, str(project))


def test_override_precedence_and_empty_removes(dirs):
    user_dir, project_dir, project = dirs
    user_dir.mkdir(parents=True)
    (user_dir / "coder.md").write_text("USER CODER {not: a template} {cwd}")
    assert load_prompt("coder", str(project)) == "USER CODER {not: a template} {cwd}"
    project_dir.mkdir(parents=True)
    (project_dir / "coder.md").write_text("PROJECT CODER")
    text, source = resolve_prompt("coder", str(project))
    assert text == "PROJECT CODER" and source == str(project_dir / "coder.md")
    main = build_system_prompt(str(project))
    assert "PROJECT CODER" in main and "USER CODER" not in main
    (project_dir / "security.md").write_text("")
    main = build_system_prompt(str(project))
    assert _first_line("security") not in main
    assert "\n\n\n\n" not in main


def test_reviewer_override_changes_review_skill(dirs):
    _, project_dir, project = dirs
    project_dir.mkdir(parents=True)
    (project_dir / "reviewer.md").write_text("Only check for typos. JSON example: {\"a\": 1}")
    body = discover_skills(str(project))["reviewer"].load_body()
    assert body.startswith("Only check for typos")


def test_plan_mode_sends_planner_prompt_once(tmp_path, fake_server, dirs):
    import json

    from coded.agent import create_agent
    from coded.config import Config, ModelConfig
    from coded.permissions import PermissionManager

    _, _, project = dirs
    seen = []

    def script(body):
        seen.append(body["messages"][-1]["content"])
        return {"role": "assistant", "content": "ok"}, {"prompt_tokens": 1, "completion_tokens": 1,
                                                         "total_tokens": 2}

    with fake_server(script) as srv:
        cfg = Config()
        model = ModelConfig(name="f", model="f", provider="openai-compatible", base_url=srv.base_url,
                            api_key="x")
        cfg.add_model(model)
        agent = create_agent(model=model, config=cfg, cwd=str(project),
                             permissions=PermissionManager(), stream=False, verbose=False)
        agent.set_mode("plan")
        agent.run("first")
        agent.run("second")
        agent.set_mode("default")
        agent.set_mode("plan")
        agent.run("third")
    planner = _first_line("planner")
    assert planner in seen[0] and planner not in seen[1] and planner in seen[2]
    assert all("[Plan mode is ON]" in s for s in seen)


def test_prompts_cli(dirs, capsys):
    from coded.cli import main

    user_dir, project_dir, project = dirs
    assert main(["prompts", "init", "reviewer", "--cwd", str(project)]) == 0
    assert (user_dir / "reviewer.md").read_text() == (builtin_prompts_dir() / "reviewer.md").read_text()
    assert main(["prompts", "init", "all", "--project", "--cwd", str(project)]) == 0
    assert sorted(p.stem for p in project_dir.glob("*.md")) == sorted(PROMPT_NAMES)
    capsys.readouterr()
    assert main(["prompts", "list", "--cwd", str(project)]) == 0
    out = capsys.readouterr().out
    assert out.count("project") >= len(PROMPT_NAMES)
    assert main(["prompts", "show", "--cwd", str(project)]) == 0
    assert "You are coded" in capsys.readouterr().out
    assert main(["prompts", "show", "nope", "--cwd", str(project)]) == 1
