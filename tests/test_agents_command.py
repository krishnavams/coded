"""`coded agents`: what is usable, what is not and why, and how to get it.

`which` is injected so every case runs on any machine, whatever is installed.
"""

from __future__ import annotations

import json
import sys

from coded.config import Config
from coded.external_agents import AGENT_INFO, PRESETS, agent_statuses, install_hint, version_of


def _which(installed):
    return lambda name: f"/usr/bin/{name}" if name in installed else None


def _by_name(statuses):
    return {s.name: s for s in statuses}


def test_installed_presets_are_ready_and_the_rest_carry_an_install_command():
    got = _by_name(agent_statuses(Config(), which=_which({"aider", "npm"}), platform="linux"))
    assert set(PRESETS) <= set(got)
    assert got["aider"].state == "ready" and got["aider"].path == "/usr/bin/aider"
    assert got["aider"].open_source is True
    assert got["codex"].state == "not installed"
    assert got["codex"].install == "npm install -g @openai/codex" and got["codex"].note == ""
    assert got["claude"].open_source is False


def test_every_preset_has_install_information():
    assert set(AGENT_INFO) == set(PRESETS)
    for name in PRESETS:
        for platform in ("linux", "darwin", "win32"):
            cmd, _ = install_hint(name, platform, which=_which({"npm", "python"}))
            assert cmd, (name, platform)


def test_missing_prerequisites_are_named():
    _, note = install_hint("gemini", "linux", which=_which(set()))
    assert "Node.js" in note
    cmd, note = install_hint("aider", "linux", which=_which({"python3"}))
    assert cmd.startswith("python3 -m pip") and note == ""


def test_platform_specific_commands():
    assert install_hint("goose", "darwin")[0] == "brew install block-goose-cli"
    assert install_hint("goose", "linux")[0].startswith("curl -fsSL https://github.com/block/goose")
    assert install_hint("cursor", "win32")[0].startswith("see ")  # no shell one-liner on Windows


def test_config_states_are_explained():
    cfg = Config()
    cfg.external_agents = {"aider": {"enabled": False},
                           "mine": {"command": ["my-agent", "{prompt}"], "description": "Ours"},
                           "ghost": {"command": ["not-here", "{prompt}"]}}
    cfg.detect_external_agents = False
    got = _by_name(agent_statuses(cfg, which=_which({"aider", "codex", "my-agent"})))
    assert got["aider"].state == "disabled"
    assert got["codex"].state == "detection off"          # installed, but not auto-offered
    assert got["mine"].state == "ready" and got["mine"].configured
    assert got["ghost"].state == "not installed" and "not on PATH" in got["ghost"].note
    assert got["ghost"].install == ""                      # we do not guess installs for custom agents


def test_the_statuses_agree_with_what_the_model_is_offered():
    from coded.external_agents import resolve_external_agents

    cfg = Config()
    ready = {s.name for s in agent_statuses(cfg) if s.state == "ready"}
    assert ready == set(resolve_external_agents(cfg))


def test_version_check_proves_the_program_starts(tmp_path):
    ok, out = version_of(sys.executable)
    assert ok and "Python" in out
    ok, out = version_of(str(tmp_path / "missing-binary"))
    assert not ok and "could not start" in out


def test_the_command_prints_install_lines_and_never_runs_them(capsys, monkeypatch, tmp_path):
    from coded import cli, external_agents

    ran = []
    monkeypatch.setattr(external_agents.shutil, "which", _which({"npm"}))
    monkeypatch.setattr(external_agents, "run_command", lambda *a, **k: ran.append(a))
    monkeypatch.setenv("CODED_CONFIG", str(tmp_path / "none.json"))
    assert cli.main(["agents", "--cwd", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "npm install -g @google/gemini-cli" in out and "To install" in out
    assert ran == []


def test_json_output_and_unknown_names(capsys, monkeypatch, tmp_path):
    from coded import cli, external_agents

    monkeypatch.setattr(external_agents.shutil, "which", _which(set()))
    monkeypatch.setenv("CODED_CONFIG", str(tmp_path / "none.json"))
    assert cli.main(["agents", "--json", "aider", "--cwd", str(tmp_path)]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["aider"] and rows[0]["state"] == "not installed"
    assert cli.main(["agents", "nope", "--cwd", str(tmp_path)]) == 2
