"""coded mission: plans first, restarts until done, stops when stuck, reports,
resumes after a kill, and never touches the user's checkout."""

from __future__ import annotations

import json
import subprocess

import pytest

from coded import mission
from coded.events import EventLog


class _Todos:
    def __init__(self):
        self.items = []


class _Perms:
    plan_mode = False


class _Agent:
    """Just enough of an Agent for run_mission: run(), events, todos,
    permissions.plan_mode and over_budget(). Records whether each run was
    in the planning phase (changes refused)."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.events = EventLog()
        self.todos = _Todos()
        self.permissions = _Perms()
        self.messages = []
        self.planning = []
        self.budget_hit = False

    def run(self, message):
        self.messages.append(message)
        self.planning.append(self.permissions.plan_mode)
        step = self.steps.pop(0)
        return step(self)

    def over_budget(self):
        return "over budget" if self.budget_hit else None


_PLAN = [{"content": "add export", "status": "pending"}]


def _plan(agent):
    agent.todos.items = [dict(t) for t in _PLAN]
    return "Plan: add export."


def _no_plan(agent):
    return "I will just do it."


def _answer(text="all done", todos=None):
    def step(agent):
        agent.todos.items = todos if todos is not None else [
            dict(t, status="completed") for t in agent.todos.items]
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


def _go(agent, **kw):
    kw.setdefault("sleep", lambda s: None)
    return mission.run_mission(agent, "goal", **kw)


def test_messages_keep_the_goal_verbatim():
    goal = "fix {braces} and [/x] markup"
    assert goal in mission.brief(goal) and goal in mission.work_message(goal)
    assert "nobody can" in mission.brief(goal) and "todo_write" in mission.brief(goal)
    assert "Do not stop to ask" in mission.work_message(goal)


def test_it_plans_with_changes_refused_then_works():
    agent = _Agent([_plan, _answer()])
    result = _go(agent)
    assert result.status == "done" and result.final == "all done"
    assert [a.outcome for a in result.attempts] == ["planned", "answered"]
    assert agent.planning == [True, False] and agent.permissions.plan_mode is False
    assert agent.messages[0] == mission.brief("goal")
    assert agent.messages[1] == mission.work_message("goal")


def test_no_task_list_is_asked_for_again_then_the_work_starts_anyway():
    agent = _Agent([_no_plan, _plan, _answer()])
    assert _go(agent).status == "done"
    assert agent.messages[1] == mission.PLAN_NUDGE and agent.planning == [True, True, False]

    agent = _Agent([_no_plan, _no_plan, _answer(todos=[])])
    result = _go(agent)
    assert [a.outcome for a in result.attempts] == ["no_plan", "answered"]
    assert agent.planning == [True, True, False]


def test_a_failed_request_waits_and_continues():
    waits = []
    agent = _Agent([_plan, _boom, _answer()])
    result = _go(agent, sleep=waits.append, backoff=(5, 10))
    assert result.status == "done" and waits == [5]
    assert [a.outcome for a in result.attempts] == ["planned", "error", "answered"]
    assert agent.messages[2].startswith(mission.CONTINUE_PREFIX)


def test_a_failure_while_planning_retries_the_plan():
    agent = _Agent([_boom, _plan, _answer()])
    assert _go(agent).status == "done"
    assert agent.messages[1] == mission.brief("goal") and agent.planning == [True, True, False]


def test_the_model_failing_every_time_gives_up_after_the_restarts():
    waits = []
    agent = _Agent([_plan] + [_boom] * 5)
    result = _go(agent, max_restarts=2, sleep=waits.append, backoff=(1, 2))
    assert result.status == "failed" and waits == [1, 2]
    assert [a.outcome for a in result.attempts] == ["planned", "error", "error", "error"]


def test_the_step_limit_is_continued_but_a_loop_is_not():
    agent = _Agent([_plan, _limit(), _answer()])
    assert _go(agent).status == "done"
    assert "step limit" in agent.messages[2]

    agent = _Agent([_plan, _limit("it does not look like progress: same `read` call")])
    result = _go(agent)
    assert result.status == "stuck" and len(agent.messages) == 2


def test_open_tasks_are_sent_back_then_reported():
    open_list = [{"content": "write tests", "status": "pending"},
                 {"content": "read code", "status": "completed"}]
    agent = _Agent([_plan] + [_answer(todos=open_list)] * 3)
    result = _go(agent, max_restarts=2)
    assert result.status == "done"
    assert [a.outcome for a in result.attempts] == ["planned", "open_tasks", "open_tasks",
                                                   "answered"]
    assert "write tests" in agent.messages[2] and "read code" not in agent.messages[2]


def test_the_budget_stops_it():
    def spend(agent):
        agent.budget_hit = True
        return "partial"
    result = _go(_Agent([_plan, spend]))
    assert result.status == "budget" and result.final == "partial"


def test_a_resumed_mission_with_a_plan_goes_straight_back_to_work():
    agent = _Agent([_answer()])
    agent.todos.items = [dict(t) for t in _PLAN]
    result = _go(agent, resume=True)
    assert result.status == "done" and agent.planning == [False]
    assert "interrupted" in agent.messages[0]
    # without a task list it plans first
    agent = _Agent([_plan, _answer()])
    _go(agent, resume=True)
    assert agent.planning == [True, False] and "interrupted" in agent.messages[0]


def test_a_failed_notification_url_does_not_raise():
    def opener(req, timeout):
        assert req.data == b"body" and req.get_method() == "POST"
        raise OSError("offline")
    assert mission.post_notification("https://ntfy.sh/x", "title", "body", opener=opener) is False


# ---------------------------------------------------------------------------
# End to end: a git repo, the CLI, the fake model planning then writing a file
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout.strip()


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


_USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def _call(cid, name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}, _USAGE


def _say(text):
    return {"role": "assistant", "content": text}, _USAGE


def _todos(status):
    return {"todos": [{"content": "add export.py", "status": status}]}


def _mission_script():
    """Plan (todo_write; a write attempted now is refused), then work."""
    steps = [
        lambda: _call("c1", "todo_write", _todos("pending")),
        lambda: _call("c2", "write", {"path": "early.py", "content": "x = 1\n"}),
        lambda: _say("Plan: add export.py."),
        lambda: _call("c3", "write", {"path": "export.py",
                                      "content": "def export():\n    return 'csv'\n"}),
        lambda: _call("c4", "todo_write", _todos("completed")),
        lambda: _say("Added export.py. Assumed CSV."),
    ]
    n = {"i": 0}

    def script(body):
        n["i"] += 1
        return steps[min(n["i"], len(steps)) - 1]()
    return script


def _config(tmp_path, server):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"models": {"fake": {"model": "fake", "base_url": server.base_url,
                                                   "api_key": "k"}},
                               "default_model": "fake"}))
    return str(cfg)


_QUIET = ["--yes", "--no-checks", "--notify", "off", "--budget", "5"]


def _report(repo):
    reports = list((repo / ".coded" / "missions").glob("*.md"))
    assert len(reports) == 1
    return reports[0].read_text(encoding="utf-8")


def test_a_mission_plans_works_on_a_branch_and_writes_a_report(repo, tmp_path, fake_server):
    with fake_server(_mission_script()) as server:
        code = mission.mission_main(["add CSV export", "--cwd", str(repo),
                                     "--config", _config(tmp_path, server)] + _QUIET)
    assert code == 0
    # the user's checkout is untouched; the change is committed on the mission branch
    assert not (repo / "export.py").exists()
    assert _git(repo, "status", "--porcelain") == ""
    branches = _git(repo, "branch", "--format=%(refname:short)").splitlines()
    branch = next(b for b in branches if b.startswith("coded/mission-"))
    shown = _git(repo, "show", "--stat", branch)
    assert "export.py" in shown and "early.py" not in shown  # nothing changed while planning
    assert _git(repo, "log", "-1", "--format=%s", branch) == "coded mission: add CSV export"
    text = _report(repo)
    assert "✅ Done" in text and "export.py" in text and "Assumed CSV" in text
    assert "- [x] add export.py" in text
    assert f"merge {branch}" in text and f"branch -D {branch}" in text
    state = mission.load_states(str(repo))[0]
    assert state["status"] == "done" and state["branch"] == branch


def test_a_killed_mission_resumes_and_still_reports(repo, tmp_path, fake_server, monkeypatch):
    real = mission.run_mission

    def killed_after_planning(agent, goal, **kw):
        agent.permissions.plan_mode = True
        agent.run(mission.brief(goal))  # todo_write, a refused write, the plan
        agent.permissions.plan_mode = False
        raise SystemExit("the computer went to sleep")

    with fake_server(_mission_script()) as server:
        monkeypatch.setattr(mission, "run_mission", killed_after_planning)
        with pytest.raises(SystemExit):
            mission.mission_main(["add CSV export", "--cwd", str(repo),
                                  "--config", _config(tmp_path, server)] + _QUIET)
        monkeypatch.setattr(mission, "run_mission", real)
    state = mission.load_states(str(repo))[0]
    assert state["status"] == "running"  # killed: no report, nothing finished
    assert not list((repo / ".coded" / "missions").glob("*.md"))
    assert mission.find_resumable(str(repo), "latest")["id"] == state["id"]

    steps = iter([_call("c3", "write", {"path": "export.py", "content": "ok = 1\n"}),
                  _call("c4", "todo_write", _todos("completed")),
                  _say("Finished after the interruption.")])
    with fake_server(lambda body: next(steps)) as server:
        cfg = _config(tmp_path, server)
        # resumed from inside the worktree: the main checkout's missions are found
        code = mission.mission_main(["--resume", "--cwd", state["work_dir"], "--config", cfg])
    assert code == 0
    state = mission.load_states(str(repo))[0]
    assert state["status"] == "done"
    text = _report(repo)
    assert "✅ Done" in text and "Finished after the interruption" in text
    assert "export.py" in _git(repo, "show", "--stat", state["branch"])


def test_resume_with_nothing_to_resume_says_so(repo):
    assert mission.mission_main(["--resume", "--cwd", str(repo)]) == 1
    assert mission.mission_main(["--list", "--cwd", str(repo)]) == 0


def test_questions_are_asked_up_front_and_a_no_is_respected(repo, tmp_path, fake_server,
                                                            monkeypatch):
    from coded import cli, trust

    (repo / "Makefile").write_text("test:\n\ttrue\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "make")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    import io

    from coded import ui

    con = ui.TranscriptConsole(width=120, record=True, file=io.StringIO(), color_system=None)
    monkeypatch.setattr(ui, "console", con)
    asked = []

    def read(prompt):
        asked.append(prompt)
        return "n"

    with fake_server(_mission_script()) as server:
        code = mission.mission_main(["add export", "--cwd", str(repo),
                                     "--config", _config(tmp_path, server),
                                     "--notify", "off", "--budget", "5"], read=read)
    assert code == 0
    shown = con.export_text()
    # Both asked in the choice panel (the same as every other prompt), answered "n".
    assert len(asked) == 2 and all("Answer (1-2)" in q for q in asked)
    assert "Mission · checks" in shown and "make test" in shown
    assert "Mission · shell commands" in shown and "No, no shell commands" in shown
    assert not trust.check_approved(str(repo), "make test")
    assert "No checks ran" in _report(repo)


def test_a_mission_needs_a_goal():
    assert mission.mission_main(["--yes"]) == 2
