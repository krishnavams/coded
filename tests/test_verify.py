"""The verification gate: a turn that changed something has to earn its answer.

The point of these tests is the *loop*, not the quality of any model's review:
a failing check sends the turn back, a passing one lets it through, and an
unconfigured session behaves exactly as it did before.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.verify import VerifySettings, run_checks, turn_diff

USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def _agent(tmp_path, base_url, **cfg_kw):
    cfg = Config(**cfg_kw)
    model = ModelConfig(name="main", model="main", provider="openai-compatible",
                        base_url=base_url, api_key="k")
    cfg.add_model(model, make_default=True)
    return create_agent(model=model, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(auto_approve=True),
                        stream=False, verbose=False)


def _write_then_answer(path, content, answer="Done."):
    """A model that writes a file on its first step, then answers."""
    def script(body):
        if any(m.get("role") == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": answer}, USAGE
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "write",
                          "arguments": json.dumps({"path": path, "content": content})}}]}, USAGE
    return script


def _checker(tmp_path, passes_after: int):
    """A check command that fails until the agent has run it `passes_after` times.

    Quote-free on purpose: Windows shells re-parse quoting differently.
    """
    counter = tmp_path / "runs.txt"
    script = tmp_path / "check.py"
    script.write_text(
        "import pathlib, sys\n"
        f"c = pathlib.Path(r'{counter}')\n"
        "n = int(c.read_text()) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        "print('check run', n + 1)\n"
        f"sys.exit(0 if n + 1 > {passes_after} else 1)\n")
    # Forward slashes, no quotes: the command goes through whichever shell is
    # resolved, and Git Bash — the usual one on Windows — treats a backslash as
    # an escape, so a native path would silently never run.
    exe = Path(sys.executable).as_posix()
    return f"{exe} {script.as_posix()}", counter


# -- the checks gate ---------------------------------------------------------

def test_a_failing_check_sends_the_turn_back_and_a_passing_one_lets_it_through(
        tmp_path, fake_server):
    command, counter = _checker(tmp_path, passes_after=1)  # fails once, then passes
    asks = []

    def script(body):
        asks.append(body["messages"][-1]["content"])
        return _write_then_answer("app.py", "x = 1")(body)

    with fake_server(script) as srv:
        agent = _agent(tmp_path, srv.base_url,
                       verify={"command": command, "max_rounds": 2})
        out = agent.run("add app.py")

    assert out == "Done."
    assert int(counter.read_text()) == 2  # ran, failed, ran again after the fix
    sent_back = [a for a in asks if isinstance(a, str) and a.startswith("[verify]")]
    assert len(sent_back) == 1
    assert "failed" in sent_back[0] and "Never weaken, skip or delete a test" in sent_back[0]


def test_a_check_that_never_passes_stops_at_max_rounds_and_says_so(tmp_path, fake_server):
    command, counter = _checker(tmp_path, passes_after=99)  # always fails
    notes = []

    with fake_server(_write_then_answer("app.py", "x = 1")) as srv:
        agent = _agent(tmp_path, srv.base_url,
                       verify={"command": command, "max_rounds": 2})
        agent.on_event = lambda ev: notes.append(ev.get("text", "")) \
            if ev.get("type") == "status" else None
        out = agent.run("add app.py")

    assert out == "Done."  # the answer is still delivered…
    # …after max_rounds attempts plus one final check, so a fix made in the last
    # round is not reported as unverified.
    assert int(counter.read_text()) == 3
    assert any("did not pass after 2 round" in n and "unverified" in n for n in notes)


def test_a_turn_that_changes_nothing_is_not_verified(tmp_path, fake_server):
    command, counter = _checker(tmp_path, passes_after=99)
    with fake_server(lambda body: ({"role": "assistant", "content": "Just answering."},
                                   USAGE)) as srv:
        agent = _agent(tmp_path, srv.base_url, verify={"command": command})
        assert agent.run("what does this do?") == "Just answering."
    assert not counter.exists()  # the checks never ran


def test_an_unconfigured_session_runs_no_checks(tmp_path, fake_server):
    with fake_server(_write_then_answer("app.py", "x = 1")) as srv:
        agent = _agent(tmp_path, srv.base_url)  # no verify section at all
        assert agent.run("add app.py") == "Done."


def test_a_shell_only_change_is_still_verified(tmp_path, fake_server):
    """`bash` edits are not checkpointed, so checkpoints cannot be the gate."""
    command, counter = _checker(tmp_path, passes_after=0)  # passes immediately

    def script(body):
        if any(m.get("role") == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "Done."}, USAGE
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "bash",
                          "arguments": json.dumps({"command": "echo hi"})}}]}, USAGE

    with fake_server(script) as srv:
        agent = _agent(tmp_path, srv.base_url, verify={"command": command})
        assert agent.run("touch it up") == "Done."
    assert int(counter.read_text()) == 1


def test_plan_mode_and_subagents_are_never_verified(tmp_path, fake_server):
    command, counter = _checker(tmp_path, passes_after=99)
    with fake_server(_write_then_answer("app.py", "x = 1")) as srv:
        agent = _agent(tmp_path, srv.base_url, verify={"command": command})
        agent.mode = "plan"
        agent.run("plan it")
        assert not counter.exists()
        agent.mode = "default"
        agent.is_subagent = True
        agent.run("do it")
    assert not counter.exists()


def test_a_check_that_cannot_run_is_reported_not_ignored(tmp_path):
    exe = Path(sys.executable).as_posix()
    result = run_checks(f"{exe} -c import sys;sys.exit(3)", str(tmp_path), 30)
    assert result.ok is False
    bad = run_checks("definitely-not-a-command-xyz", str(tmp_path), 30)
    assert bad.ok is False  # a missing command is a failure, never a pass


# -- the second opinion ------------------------------------------------------

def test_the_second_opinion_only_runs_when_a_verify_model_is_named(tmp_path, fake_server):
    """A 7B model must not be promoted to critic just because it is configured."""
    seen = []

    def script(body):
        seen.append(body["model"])
        return _write_then_answer("app.py", "x = 1")(body)

    with fake_server(script) as srv:
        agent = _agent(tmp_path, srv.base_url, verify={"second_opinion": True})
        agent.run("add app.py")
        assert seen.count("main") == 2 and len(seen) == 2  # no reviewer call

        # Same config plus a named verify model: now it is asked. (A fresh agent,
        # because the script decides what to do from the conversation so far.)
        other = _agent(tmp_path, srv.base_url, verify={"second_opinion": True})
        other.config.models["critic"] = ModelConfig(
            name="critic", model="critic", provider="openai-compatible",
            base_url=srv.base_url, api_key="k")
        other.config.model_roles["verify"] = "critic"
        other.run("add app.py again")
    assert "critic" in seen


def test_a_reviewer_asking_for_changes_sends_the_turn_back_once(tmp_path, fake_server):
    rounds = {"review": 0}
    asks = []

    def script(body):
        if body["model"] == "critic":
            rounds["review"] += 1
            if rounds["review"] == 1:
                return ({"role": "assistant", "content":
                         "VERDICT: NEEDS_CHANGES\napp.py: x is never used."}, USAGE)
            return {"role": "assistant", "content": "VERDICT: PASS"}, USAGE
        asks.append(body["messages"][-1]["content"])
        return _write_then_answer("app.py", "x = 1")(body)

    with fake_server(script) as srv:
        agent = _agent(tmp_path, srv.base_url, verify={"second_opinion": True})
        agent.config.models["critic"] = ModelConfig(
            name="critic", model="critic", provider="openai-compatible",
            base_url=srv.base_url, api_key="k")
        agent.config.model_roles["verify"] = "critic"
        assert agent.run("add app.py") == "Done."

    assert rounds["review"] == 2
    back = [a for a in asks if isinstance(a, str) and a.startswith("[verify]")]
    assert len(back) == 1 and "x is never used" in back[0]
    assert "may be mistaken" in back[0]  # the agent may push back, not just comply


def test_an_unparseable_verdict_costs_no_extra_round(tmp_path, fake_server):
    calls = {"critic": 0}

    def script(body):
        if body["model"] == "critic":
            calls["critic"] += 1
            return {"role": "assistant", "content": "I think it's probably fine?"}, USAGE
        return _write_then_answer("app.py", "x = 1")(body)

    with fake_server(script) as srv:
        agent = _agent(tmp_path, srv.base_url, verify={"second_opinion": True})
        agent.config.models["critic"] = ModelConfig(
            name="critic", model="critic", provider="openai-compatible",
            base_url=srv.base_url, api_key="k")
        agent.config.model_roles["verify"] = "critic"
        assert agent.run("add app.py") == "Done."
    assert calls["critic"] == 1  # asked once, answer unusable, turn released


def test_a_reviewer_outage_never_fails_the_turn(tmp_path, fake_server):
    def script(body):
        if body["model"] == "critic":
            raise AssertionError("the reviewer endpoint is down")
        return _write_then_answer("app.py", "x = 1")(body)

    with fake_server(script) as srv:
        agent = _agent(tmp_path, srv.base_url, verify={"second_opinion": True})
        agent.config.models["critic"] = ModelConfig(
            name="critic", model="critic", provider="openai-compatible",
            base_url=srv.base_url, api_key="k", max_retries=0)
        agent.config.model_roles["verify"] = "critic"
        assert agent.run("add app.py") == "Done."


# -- the diff the reviewer sees ----------------------------------------------

def test_the_diff_covers_what_the_turn_changed(tmp_path, fake_server):
    target = tmp_path / "app.py"
    target.write_text("old = 1\n")

    with fake_server(_write_then_answer("app.py", "new = 2\n")) as srv:
        agent = _agent(tmp_path, srv.base_url)
        agent.run("change it")
        diff, paths = turn_diff(agent.checkpoints, str(tmp_path))

    assert paths == ["app.py"]
    assert "-old = 1" in diff and "+new = 2" in diff


def test_settings_survive_nonsense_values():
    s = VerifySettings.from_config({"timeout": "soon", "max_rounds": "lots", "command": ""})
    assert s.timeout == 300.0 and s.max_rounds == 2 and s.command is None
    assert s.runs_checks() is False
    assert VerifySettings.from_config({"max_rounds": -1}).max_rounds == 0


def test_a_fix_made_in_the_last_round_is_not_reported_unverified(tmp_path, fake_server):
    """With max_rounds: 1 the final fix had never been checked before being
    called unverified."""
    command, counter = _checker(tmp_path, passes_after=1)  # fails once, then passes
    notes = []

    with fake_server(_write_then_answer("app.py", "x = 1")) as srv:
        agent = _agent(tmp_path, srv.base_url,
                       verify={"command": command, "max_rounds": 1})
        agent.on_event = lambda ev: notes.append(ev.get("text", "")) \
            if ev.get("type") == "status" else None
        assert agent.run("add app.py") == "Done."

    assert int(counter.read_text()) == 2
    assert not any("unverified" in n for n in notes)
    assert any("Checks passed" in n for n in notes)
