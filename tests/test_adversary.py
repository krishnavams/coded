"""Adversarial review: read-only sub-agent, evidence-checked findings, opt-in phase."""

from __future__ import annotations

from coded import adversary
from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.skills import discover_skills

_U = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}

REPORT = """Looked at the change.

FINDING
severity: high
location: app.py:2
claim: The path from the request is joined without checking it stays in uploads/.
trigger: filename "../../etc/passwd" in the POST body
impact: reads any file the server can

FINDING
severity: medium
location: ghost.py:10
claim: Made-up file.
trigger: anything at all really

FINDING
severity: low
location: app.py:1
claim: Something vague.
trigger:

FINDING
severity: critical!!
location: app.py:1
claim: Bad severity.
trigger: an empty list of items
"""


def test_findings_without_evidence_are_dropped(tmp_path):
    (tmp_path / "app.py").write_text("import os\nopen(os.path.join('uploads', name))\n")
    found, dropped = adversary.parse(REPORT, str(tmp_path))
    assert [(f.severity, f.location) for f in found] == [("high", "app.py:2")]
    assert dropped == 3
    text = adversary.render(found, dropped, "note", REPORT)
    assert "[HIGH] `app.py:2`" in text and "3 claim(s) dropped" in text
    assert "No findings with evidence" in adversary.render([], 0, "note", "NO FINDINGS")


def _agent(tmp_path, base_url, roles=None):
    cfg = Config()
    main = ModelConfig(name="main", model="main", base_url=base_url, api_key="k")
    cfg.add_model(main, make_default=True)
    cfg.add_model(ModelConfig(name="critic", model="critic", base_url=base_url, api_key="k"))
    cfg.model_roles = roles or {}
    return create_agent(model=main, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(), stream=False, verbose=False,
                        skills=discover_skills(str(tmp_path)))


def test_runs_read_only_with_a_fresh_context_and_says_which_model(tmp_path, fake_server):
    (tmp_path / "app.py").write_text("import os\nopen(os.path.join('uploads', name))\n")
    seen = []

    def script(body):
        seen.append(body)
        return {"role": "assistant", "content": REPORT}, _U

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url)
        agent.session.add_user("SECRET author transcript")
        out = adversary.run(agent, "app.py")
        tools = {t["function"]["name"] for t in seen[-1]["tools"]}
        assert not tools & {"write", "edit", "multi_edit", "apply_patch", "notebook_edit",
                            "delete", "move"}
        assert {"read", "grep", "bash", "git"} <= tools
        assert "SECRET" not in str(seen[-1]["messages"])  # not the author's context
        assert seen[-1]["model"] == "main" and "same model as the main agent" in out
        agent2 = _agent(tmp_path, server.base_url, roles={"review": "critic"})
        out2 = adversary.run(agent2, "app.py")
        assert seen[-1]["model"] == "critic" and "independent of the main model" in out2
    assert "[HIGH]" in out and "ghost.py" not in out


def test_adversarial_phase_is_opt_in(tmp_path, monkeypatch):
    from coded import cli, review

    phases_seen = []

    def fake_run_review(agent, target, phases=None, verbose=True):
        phases_seen.append(list(phases))
        return "# Review summary\n"

    monkeypatch.setattr(review, "run_review", fake_run_review)
    monkeypatch.setattr(cli, "synthesize_verdict", lambda a, r: ("APPROVE", "ok"), raising=False)
    cfg_path = tmp_path / "c.json"
    cfg_path.write_text('{"models": {"m": {"provider": "openai", "model": "m", "api_key": "k"}}, '
                        '"default_model": "m"}')
    base = ["--cwd", str(tmp_path), "--config", str(cfg_path), "--no-verdict"]
    cli._handle_review_command(base)
    cli._handle_review_command(base + ["--adversarial"])
    assert phases_seen[0] == review.REVIEW_PHASES
    assert phases_seen[1] == review.REVIEW_PHASES + ["adversary"]
