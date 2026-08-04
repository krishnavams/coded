"""Test the /review pipeline (reviewer → qa → security sub-agents)."""

from __future__ import annotations

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.review import REVIEW_PHASES, run_review
from coded.skills import discover_skills


def _agent(tmp_path, base_url):
    cfg = Config()
    model = ModelConfig(name="fake", model="fake", provider="openai-compatible",
                        base_url=base_url, api_key="k")
    cfg.add_model(model, make_default=True)
    return create_agent(
        model=model, config=cfg, cwd=str(tmp_path),
        permissions=PermissionManager(auto_approve=True),
        skills=discover_skills(str(tmp_path)), stream=False, verbose=False,
    )


def test_review_runs_all_three_phases(tmp_path, fake_server):
    calls = {"n": 0}

    def script(body):
        calls["n"] += 1
        usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
        # Each phase's sub-agent returns a plain report (no tool calls).
        return {"role": "assistant", "content": "PHASE REPORT OK"}, usage

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url)
        out = run_review(agent, target="src/app.py", verbose=False)

    assert calls["n"] == 3  # one model call per phase (no tools)
    for phase in REVIEW_PHASES:
        assert f"## {phase}" in out
    assert out.count("PHASE REPORT OK") == 3
    assert "src/app.py" in out


def test_review_skips_missing_role(tmp_path, fake_server):
    def script(body):
        return {"role": "assistant", "content": "R"}, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url)
        # Remove one role to prove the pipeline degrades gracefully.
        agent.skills.pop("qa", None)
        out = run_review(agent, target="x", verbose=False)

    assert "## qa" in out and "skipped" in out
    assert "## reviewer" in out and "## security" in out


def _verdict_script(verdict_word):
    """Fake server: phases return a report; the verdict call returns a verdict."""
    def script(body):
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        msgs = body.get("messages", [])
        is_gate = any("release gate" in (m.get("content") or "") for m in msgs
                      if isinstance(m.get("content"), str))
        if is_gate:
            return {"role": "assistant", "content": f"VERDICT: {verdict_word}\nBecause reasons."}, usage
        return {"role": "assistant", "content": "phase findings"}, usage
    return script


def test_review_cli_passes_on_approve(tmp_path, fake_server):
    from coded.cli import main

    with fake_server(_verdict_script("APPROVE")) as server:
        code = main(["review", "src.py", "--cwd", str(tmp_path),
                     "--base-url", server.base_url, "--api-key", "k", "--model-name", "fake"])
    assert code == 0


def test_review_cli_fails_on_needs_changes(tmp_path, fake_server):
    from coded.cli import main

    out_file = tmp_path / "report.md"
    with fake_server(_verdict_script("NEEDS_CHANGES")) as server:
        code = main(["review", "--cwd", str(tmp_path), "--output", str(out_file),
                     "--base-url", server.base_url, "--api-key", "k", "--model-name", "fake"])
    assert code == 1
    assert out_file.is_file()
    assert "verdict" in out_file.read_text().lower()


def test_review_cli_no_fail_flag(tmp_path, fake_server):
    from coded.cli import main

    with fake_server(_verdict_script("NEEDS_CHANGES")) as server:
        code = main(["review", "--cwd", str(tmp_path), "--fail-on", "never",
                     "--base-url", server.base_url, "--api-key", "k", "--model-name", "fake"])
    assert code == 0


def test_review_without_skills_reports_gracefully(tmp_path, fake_server):
    def script(body):
        return {"role": "assistant", "content": "R"}, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url)
        agent.skills = {}
        out = run_review(agent, verbose=False)

    assert "No role skills" in out
