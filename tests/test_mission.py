"""coded mission: restarts until done, stops when stuck, reports, never touches the checkout."""

from __future__ import annotations

import json
import subprocess

import pytest

from coded import mission
from coded.events import EventLog


class _Todos:
    def __init__(self):
        self.items = []


class _Agent:
    """Just enough of an Agent for run_mission: run(), events, todos, over_budget()."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.events = EventLog()
        self.todos = _Todos()
        self.messages = []
        self.budget_hit = False

    def run(self, message):
        self.messages.append(message)
        step = self.steps.pop(0)
        return step(self)

    def over_budget(self):
        return "over budget" if self.budget_hit else None


def _answer(text="all done", todos=None):
    def step(agent):
        if todos is not None:
            agent.todos.items = todos
        agent.events.add("answer", chars=len(text))
        return text
    return step


def _limit(reason="20 steps is the most auto-extension allows"):
    def step(agent):
        agent.events.add("max_turns", limit=20, reason=reason)
        return ""
    return step


def _boom(agent):
    raise RuntimeError("Connection refused")


def test_brief_keeps_the_goal_verbatim_and_says_nobody_can_answer():
    text = mission.brief("fix {braces} and [/x] markup")
    assert "fix {braces} and [/x] markup" in text
    assert "nobody can answer" in text and "todo_write" in text


def test_a_clean_answer_is_done_in_one_run():
    agent = _Agent([_answer()])
    result = mission.run_mission(agent, "goal", sleep=lambda s: None)
    assert result.status == "done" and result.final == "all done"
    assert [a.outcome for a in result.attempts] == ["answered"]


def test_a_failed_request_waits_and_continues():
    waits = []
    agent = _Agent([_boom, _answer()])
    result = mission.run_mission(agent, "goal", sleep=waits.append, backoff=(5, 10))
    assert result.status == "done" and waits == [5]
    assert [a.outcome for a in result.attempts] == ["error", "answered"]
    assert agent.messages[1].startswith(mission.CONTINUE_PREFIX)


def test_the_model_failing_every_time_gives_up_after_the_restarts():
    waits = []
    agent = _Agent([_boom] * 5)
    result = mission.run_mission(agent, "goal", max_restarts=2, sleep=waits.append,
                                 backoff=(1, 2))
    assert result.status == "failed" and len(result.attempts) == 3 and waits == [1, 2]


def test_the_step_limit_is_continued_but_a_loop_is_not():
    agent = _Agent([_limit(), _answer()])
    result = mission.run_mission(agent, "goal", sleep=lambda s: None)
    assert result.status == "done"
    assert "step limit" in agent.messages[1]

    agent = _Agent([_limit("it does not look like progress: it repeated the same `read` call")])
    result = mission.run_mission(agent, "goal", sleep=lambda s: None)
    assert result.status == "stuck" and len(agent.messages) == 1


def test_open_tasks_are_sent_back_once_then_reported():
    open_list = [{"content": "write tests", "status": "pending"},
                 {"content": "read code", "status": "completed"}]
    agent = _Agent([_answer(todos=open_list), _answer(todos=open_list), _answer(todos=open_list)])
    result = mission.run_mission(agent, "goal", max_restarts=2, sleep=lambda s: None)
    assert result.status == "done"
    assert [a.outcome for a in result.attempts] == ["open_tasks", "open_tasks", "answered"]
    assert "write tests" in agent.messages[1] and "read code" not in agent.messages[1]


def test_the_budget_stops_it():
    def spend(agent):
        agent.budget_hit = True
        return "partial"
    result = mission.run_mission(_Agent([spend]), "goal", sleep=lambda s: None)
    assert result.status == "budget" and result.final == "partial"


def test_a_failed_notification_url_does_not_raise():
    def opener(req, timeout):
        assert req.data == b"body" and req.get_method() == "POST"
        raise OSError("offline")
    assert mission.post_notification("https://ntfy.sh/x", "title", "body", opener=opener) is False


# ---------------------------------------------------------------------------
# End to end: a git repo, the CLI, the fake model writing a file
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(key, value)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.py").write_text("print('hi')\n")
    (root / ".gitignore").write_text(".coded/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _write_then_answer():
    n = {"i": 0}
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    def script(body):
        n["i"] += 1
        if n["i"] == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "write", "arguments": json.dumps(
                    {"path": "export.py", "content": "def export():\n    return 'csv'\n"})}}]}, usage
        return {"role": "assistant", "content": "Added export.py. Assumed CSV."}, usage
    return script


def test_a_mission_works_on_a_branch_and_writes_a_report(repo, tmp_path, fake_server):
    with fake_server(_write_then_answer()) as server:
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"models": {"fake": {"model": "fake", "base_url": server.base_url,
                                                       "api_key": "k"}},
                                   "default_model": "fake"}))
        code = mission.mission_main(["add CSV export", "--cwd", str(repo), "--config", str(cfg),
                                     "--yes", "--no-checks", "--notify", "off", "--budget", "5"])
    assert code == 0
    # the user's checkout is untouched; the change is committed on the mission branch
    assert not (repo / "export.py").exists()
    assert _git(repo, "status", "--porcelain") == ""
    branches = _git(repo, "branch", "--format=%(refname:short)").splitlines()
    branch = next(b for b in branches if b.startswith("coded/mission-"))
    assert "export.py" in _git(repo, "show", "--stat", branch)
    assert _git(repo, "log", "-1", "--format=%s", branch) == "coded mission: add CSV export"
    # one report, with the summary and the commands to accept or discard
    reports = list((repo / ".coded" / "missions").glob("*.md"))
    assert len(reports) == 1
    text = reports[0].read_text()
    assert "✅ Done" in text and "export.py" in text and "Assumed CSV" in text
    assert f"merge {branch}" in text and f"branch -D {branch}" in text


def test_a_mission_needs_a_goal(capsys):
    assert mission.mission_main(["--yes"]) == 2


def test_questions_are_asked_up_front_and_a_no_is_respected(repo, tmp_path, fake_server,
                                                            monkeypatch):
    from coded import cli, trust

    (repo / "Makefile").write_text("test:\n\ttrue\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "make")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    asked = []

    def read(prompt):
        asked.append(prompt)
        return "n"

    with fake_server(_write_then_answer()) as server:
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"models": {"fake": {"model": "fake", "base_url": server.base_url,
                                                       "api_key": "k"}},
                                   "default_model": "fake"}))
        code = mission.mission_main(["add export", "--cwd", str(repo), "--config", str(cfg),
                                     "--notify", "off", "--budget", "5"], read=read)
    assert code == 0
    assert any("make test" in q for q in asked) and any("shell commands" in q for q in asked)
    assert not trust.check_approved(str(repo), "make test")
    text = next((repo / ".coded" / "missions").glob("*.md")).read_text()
    assert "No checks ran" in text
