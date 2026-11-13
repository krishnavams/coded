"""`coded mission "<goal>"`: an unattended run that ends with a report.

Everything that needs a person is asked up front (project trust, the
project's checks, shell commands); then the agent works on its own in a git
worktree on a fresh branch, so the user's checkout is never touched. It plans
with the todo list, the project's checks gate every change (the verify loop),
and the step limit grows while it makes progress. When a run stops early —
the model failed, the step limit was hit, tasks were left open — the mission
sends it back to continue, up to `max_restarts` times. Nothing ever waits for
an answer: a permission nobody approved is refused, and `ask_user` makes the
model state an assumption.

The end is one Markdown report (saved under `<repo>/.coded/missions/`):
status, what changed, checks, tasks, cost, problems and the exact commands to
accept or discard the branch, plus a notification (bell/desktop, or a POST to
`--notify-url`, e.g. an ntfy.sh topic for a phone).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from coded import ui

DEFAULT_RESTARTS = 3
BACKOFF_SECONDS = (30, 120, 300)

# Built by concatenation, never str.format: the goal is free text.
BRIEF_HEAD = (
    "You are running unattended as a coded mission: nobody is watching and nobody can "
    "answer questions until it is over.\n\nGoal:\n"
)
BRIEF_TAIL = """

How to work:
1. Look around first, then write a task list with todo_write. Keep it current.
2. Work through it step by step. Make the smallest changes that reach the goal and follow \
the project's conventions.
3. When a question has no clear answer, pick the safest reasonable option, write the \
assumption down in your final summary, and keep going. Do not stop to ask.
4. Run the project's tests or checks before you finish, and fix what fails.
5. Stay inside this checkout. Do not push, publish, deploy or contact anyone.
6. If an action is refused, find another way or leave that part undone and say why.
7. Finish with a short summary: what changed, how it was verified, assumptions, and \
anything left undone with the reason."""

CONTINUE_PREFIX = "[mission] You stopped before the goal was done ("
CONTINUE_SUFFIX = ("). Continue from where you left off: check the task list, finish the "
                   "open items (or mark why one cannot be done), run the checks, then give "
                   "the final summary.")

STATUS_TEXT = {
    "done": "Done",
    "stuck": "Stopped: it was not making progress",
    "budget": "Stopped: the budget ran out",
    "failed": "Stopped: the model kept failing",
    "interrupted": "Stopped: interrupted",
}


def brief(goal: str) -> str:
    return BRIEF_HEAD + goal.strip() + BRIEF_TAIL


def continue_message(why: str) -> str:
    return CONTINUE_PREFIX + why + CONTINUE_SUFFIX


@dataclass
class Attempt:
    number: int
    outcome: str        # "answered", "max_turns", "error", "open_tasks", "budget", "interrupted"
    detail: str = ""


@dataclass
class MissionResult:
    status: str
    final: str = ""
    attempts: List[Attempt] = field(default_factory=list)
    seconds: float = 0.0


def _open_todos(agent) -> List[dict]:
    return [t for t in (agent.todos.items or [])
            if str(t.get("status", "")).lower() not in ("completed", "done", "cancelled")]


def _new_events(agent, before: List[dict]) -> List[dict]:
    """Events added since `before` was taken (the log is bounded, so match the
    last known event by identity rather than by index)."""
    now = agent.events.snapshot()
    if not before:
        return now
    last = before[-1]
    for i in range(len(now) - 1, -1, -1):
        if now[i] is last or now[i] == last:
            return now[i + 1:]
    return now


def run_mission(agent, goal: str, *, max_restarts: int = DEFAULT_RESTARTS,
                sleep: Callable[[float], None] = time.sleep,
                backoff=BACKOFF_SECONDS,
                on_attempt: Optional[Callable[[Attempt], None]] = None) -> MissionResult:
    """Drive the agent until the goal is done or it cannot go on."""
    from coded.llm import explain_failure

    started = time.monotonic()
    result = MissionResult(status="done")
    message = brief(goal)
    failures = 0

    def record(outcome: str, detail: str = "") -> Attempt:
        attempt = Attempt(len(result.attempts) + 1, outcome, detail)
        result.attempts.append(attempt)
        if on_attempt:
            on_attempt(attempt)
        return attempt

    while True:
        before = agent.events.snapshot()
        try:
            final = agent.run(message)
        except KeyboardInterrupt:
            record("interrupted")
            result.status = "interrupted"
            break
        except Exception as exc:  # noqa: BLE001 - an outage is retried, then reported
            why = explain_failure(exc)
            record("error", why)
            failures += 1
            if len(result.attempts) > max_restarts:
                result.status = "failed"
                result.final = why
                break
            sleep(backoff[min(failures - 1, len(backoff) - 1)])
            message = continue_message("the model request failed: " + why.splitlines()[0])
            continue
        result.final = final or result.final
        events = _new_events(agent, before)
        kinds = [e.get("kind") for e in events]
        if agent.over_budget():
            record("budget", agent.over_budget() or "")
            result.status = "budget"
            break
        if "interrupt" in kinds:
            record("interrupted")
            result.status = "interrupted"
            break
        if "max_turns" in kinds:
            stop = [e for e in events if e.get("kind") == "max_turns"][-1]
            why = str(stop.get("reason") or "the step limit was reached")
            record("max_turns", why)
            if len(result.attempts) > max_restarts or "not look like progress" in why:
                result.status = "stuck"
                break
            message = continue_message("step limit: " + why)
            continue
        open_items = _open_todos(agent)
        if open_items and len(result.attempts) < max_restarts:
            names = "; ".join(str(t.get("content", ""))[:60] for t in open_items[:5])
            record("open_tasks", names)
            message = continue_message(f"{len(open_items)} task(s) still open: {names}")
            continue
        record("answered")
        result.status = "done"
        break
    result.seconds = time.monotonic() - started
    return result


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _git(cwd: str, *args: str) -> str:
    from coded.worktree import _git as git

    try:
        return git(cwd, *args)
    except Exception:  # noqa: BLE001 - the report shows what it can
        return ""


def commit_changes(worktree: str, goal: str) -> str:
    """Commit everything the mission changed on its branch. Returns a note
    for the report ("" when committed or nothing changed)."""
    from coded.worktree import WorktreeError, _git as git

    if not _git(worktree, "status", "--porcelain"):
        return ""
    title = " ".join(goal.split())[:60]
    try:
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", f"coded mission: {title}")
        return ""
    except (WorktreeError, OSError) as exc:
        return f"The changes are not committed ({exc}); they are in the worktree as files."


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _checks_line(events: List[dict], command: Optional[str]) -> str:
    verify = [e for e in events if e.get("kind") == "verify"]
    if not command:
        return "No checks ran: no test command was found or approved for this project."
    if not verify:
        return f"`{command}` did not run (no change was made that needed checking)."
    last = verify[-1].get("result")
    if last == "pass":
        return f"`{command}` passed."
    if last == "stall":
        return f"`{command}` still fails the same way after fixes (gave up on it)."
    return f"`{command}` failed on the last run."


def build_report(*, goal: str, result: MissionResult, agent, branch: str = "",
                 worktree: str = "", root: str = "", base: str = "",
                 check_command: Optional[str] = None, commit_note: str = "") -> str:
    status = STATUS_TEXT.get(result.status, result.status)
    icon = "✅" if result.status == "done" else "⚠️"
    events = agent.events.snapshot()
    cost = agent.session.estimated_cost(agent.model)
    lines = [f"# Mission: {' '.join(goal.split())[:100]}", "",
             f"**{icon} {status}** · {_duration(result.seconds)} · about ${cost:.2f} · "
             f"model `{agent.model.name}` · {len(result.attempts)} run(s)", ""]
    if branch:
        lines += [f"- Branch: `{branch}` (from `{base[:10]}`)", f"- Worktree: `{worktree}`"]
    else:
        lines.append(f"- Worked directly in `{agent.cwd}` (no worktree)")
    lines.append("")

    lines += ["## What changed", ""]
    stat = _git(worktree, "diff", "--stat", f"{base}..HEAD") if branch and base else \
        _git(agent.cwd, "diff", "--stat")
    lines += (["```", stat, "```"] if stat else ["No file changes."])
    if commit_note:
        lines += ["", commit_note]
    lines.append("")

    lines += ["## Checks", "", _checks_line(events, check_command), ""]

    todos = list(agent.todos.items or [])
    if todos:
        lines += ["## Tasks", ""]
        for t in todos:
            done = str(t.get("status", "")).lower() in ("completed", "done")
            lines.append(f"- [{'x' if done else ' '}] {t.get('content', '')}")
        lines.append("")

    problems = [a for a in result.attempts if a.outcome != "answered"]
    errors = [e for e in events if e.get("kind") == "tool" and e.get("error")]
    denied = [e for e in events if e.get("kind") == "tool" and e.get("denied")]
    if problems or errors or denied:
        lines += ["## Problems on the way", ""]
        for a in problems:
            lines.append(f"- Run {a.number}: {a.outcome.replace('_', ' ')}"
                         + (f" — {a.detail.splitlines()[0][:200]}" if a.detail else ""))
        if errors:
            lines.append(f"- {len(errors)} tool call(s) failed and were retried or worked around.")
        if denied:
            names = sorted({str(e.get('name')) for e in denied})
            lines.append(f"- {len(denied)} action(s) were refused because nobody could approve "
                         f"them ({', '.join(names)}).")
        lines.append("")

    if result.final:
        lines += ["## The agent's summary", "", result.final.strip(), ""]

    lines += ["## Next steps", ""]
    if branch:
        lines += [f"- Review: `git -C {root} diff {base[:10]}..{branch}`",
                  f"- Accept: `git -C {root} merge {branch}`",
                  f"- Discard: `git -C {root} worktree remove --force {worktree} && "
                  f"git -C {root} branch -D {branch}`",
                  f"- Keep going: `cd {worktree} && coded --continue`"]
    else:
        lines += ["- Review the changes with `git diff`.", "- Keep going: `coded --continue`"]
    return "\n".join(lines) + "\n"


def save_report(root: str, text: str, stamp: Optional[str] = None) -> Path:
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    path = Path(root) / ".coded" / "missions" / f"{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def post_notification(url: str, title: str, body: str, opener=None) -> bool:
    """POST the summary to `url` (plain text; ntfy.sh topics show it on a phone)."""
    import urllib.request

    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                 headers={"Title": title.encode("ascii", "replace").decode(),
                                          "Content-Type": "text/plain; charset=utf-8"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=15):
            return True
    except Exception:  # noqa: BLE001 - a failed notification must not fail the mission
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coded mission",
        description="Work on a goal unattended in a separate git worktree and finish with "
                    "a report. Everything that needs you is asked before it starts.")
    p.add_argument("goal", nargs="*", help="What to do (or pipe it on stdin).")
    p.add_argument("-m", "--model", help="Model alias to use.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Repository directory.")
    p.add_argument("--budget", type=float, help="Stop when the cost reaches this many dollars.")
    p.add_argument("--max-restarts", type=int, default=DEFAULT_RESTARTS,
                   help=f"How often to send a stopped run back to work (default "
                        f"{DEFAULT_RESTARTS}).")
    p.add_argument("--here", action="store_true",
                   help="Work in this checkout instead of a new worktree.")
    p.add_argument("--allow-bash", action="store_true",
                   help="Run shell commands without asking (default: asked once up front).")
    p.add_argument("--yolo", action="store_true",
                   help="Approve every action (deny rules still win).")
    p.add_argument("--no-checks", action="store_true", help="Do not run the project's checks.")
    p.add_argument("--notify", choices=("bell", "desktop", "off"),
                   help="How to tell you it is over (default: config notify, else desktop).")
    p.add_argument("--notify-url", help="Also POST the summary here (e.g. https://ntfy.sh/<topic>).")
    p.add_argument("--trust-project", action="store_true",
                   help="Apply this project's full .coded/config.json without asking.")
    p.add_argument("--no-mcp", action="store_true", help="Do not start MCP servers.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Do not ask anything up front (no TTY needed); unapproved checks "
                        "and shell commands stay off unless allowed by flags.")
    return p


def _yes(question: str, read=input, default: bool = True) -> bool:
    try:
        answer = read(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in ("y", "yes")


def mission_main(argv: List[str], read=input) -> int:
    from coded import cli, notify, trust
    from coded.config import ConfigError, load_config
    from coded.permissions import PermissionManager

    args = build_parser().parse_args(argv)
    goal = " ".join(args.goal).strip()
    if not goal:
        try:
            if not sys.stdin.isatty():
                goal = sys.stdin.read().strip()
        except (OSError, ValueError):  # no usable stdin
            pass
    if not goal:
        ui.error('What should the mission do? coded mission "add CSV export to reports"')
        return 2
    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    asking = not args.yes and cli._stdin_is_tty()

    # 1. Everything that needs a person, before it starts.
    try:
        cfg = load_config(cwd=cwd, config_path=args.config,
                          trust_project=True if args.trust_project else None)
        if cfg.untrusted_project:
            if asking and cli._ask_project_trust(cfg.untrusted_project, read=read):
                cfg = load_config(cwd=cwd, config_path=args.config, trust_project=True)
            else:
                cli._note_untrusted(cfg.untrusted_project, asked=asking)
        model, pool = cli._select_model(cfg, args.model)
    except ConfigError as exc:
        ui.error(str(exc))
        return 1

    check_command = None
    if not args.no_checks:
        from coded.verify import VerifySettings, detect_checks

        check_command = VerifySettings.from_config(cfg.verify).command or detect_checks(cwd)
        if check_command and not VerifySettings.from_config(cfg.verify).command \
                and not trust.check_approved(cwd, check_command):
            if asking and _yes(f"Run the project's checks after each change?\n  "
                               f"{check_command}\n(This runs the repository's own code.)", read):
                trust.approve_check(cwd, check_command)
            else:
                check_command = None
    allow_bash = args.allow_bash or args.yolo
    if not allow_bash and asking:
        allow_bash = _yes("Let the mission run shell commands (tests, builds) without asking?"
                          + ("" if args.here else " It works in a separate worktree."), read)
    if args.budget is not None:
        cfg.max_cost = args.budget
    elif cfg.max_cost is None:
        ui.warn("No budget set: the mission can spend until it is done. "
                "Pass --budget <dollars> to cap it.")

    # 2. Where it works.
    branch = worktree = base = ""
    root = cwd
    work_dir = cwd
    if not args.here:
        from coded.worktree import WorktreeError, create_worktree

        try:
            work_dir, branch, root, base = create_worktree(cwd, label="mission-" + goal[:20])
        except WorktreeError as exc:
            ui.error(f"Cannot create a worktree ({exc}). Use --here to work in this checkout.")
            return 1
        worktree = _git(work_dir, "rev-parse", "--show-toplevel") or work_dir
        memory = Path(cwd) / ".coded" / "memory.json"
        if memory.is_file():  # project notes help; the copy is read-only in effect
            (Path(work_dir) / ".coded").mkdir(exist_ok=True)
            shutil.copy2(memory, Path(work_dir) / ".coded" / "memory.json")

    if check_command:
        cfg.verify = dict(cfg.verify or {}, command=check_command)
    rules = {k: list(v) for k, v in (cfg.permissions or {}).items() if isinstance(v, list)}
    if allow_bash:
        rules["allow"] = list(rules.get("allow") or []) + ["bash", "bash_output", "kill_shell"]
    permissions = PermissionManager(auto_approve=bool(args.yolo), rules=rules,
                                    injection_guard=cfg.injection_guard)
    permissions.accept_edits = True  # nobody is there to approve an edit
    mcp_manager, extra_tools = cli._init_mcp(cfg, disabled=args.no_mcp)

    from coded.agent import create_agent
    from coded.sessions_store import SessionStore
    from coded.skills import discover_skills

    agent = create_agent(model=model, config=cfg, cwd=work_dir, permissions=permissions,
                         extra_tools=extra_tools, skills=discover_skills(work_dir),
                         stream=False, verbose=True, pool=pool)
    if args.model:
        agent.set_routing(False)
    store = SessionStore()
    session_id = store.new_id()

    def autosave() -> None:
        agent.session.todos = [dict(t) for t in agent.todos.items]
        try:
            store.save(agent.session, session_id=session_id, cwd=work_dir, model=model.name)
        except OSError:
            pass

    agent.on_step = autosave
    ui.info(f"Mission started{f' on branch {branch}' if branch else ''}. "
            "You can walk away; Ctrl-C stops it.")

    def note(attempt: Attempt) -> None:
        if attempt.outcome != "answered":
            ui.warn(f"Run {attempt.number} ended: {attempt.outcome.replace('_', ' ')}"
                    + (f" ({attempt.detail.splitlines()[0][:120]})" if attempt.detail else "")
                    + ".")

    # 3. The work, then the report.
    try:
        result = run_mission(agent, goal, max_restarts=max(0, args.max_restarts),
                             on_attempt=note)
    finally:
        if mcp_manager is not None:
            mcp_manager.stop()
        autosave()
    commit_note = commit_changes(worktree, goal) if branch else ""
    report = build_report(goal=goal, result=result, agent=agent, branch=branch,
                          worktree=worktree, root=root, base=base,
                          check_command=check_command, commit_note=commit_note)
    path = save_report(root, report)
    ui.rule("Mission report")
    ui.assistant_markdown(report)
    ui.info(f"Report saved to {path}")

    title = f"coded mission: {STATUS_TEXT.get(result.status, result.status)}"
    summary = f"{' '.join(goal.split())[:80]} — report: {path}"
    # A mission's end is worth more than a bell: desktop unless notify is off.
    mode = args.notify or ("off" if cfg.notify == "off" else "desktop")
    notify.send(title, summary, mode)
    if args.notify_url and not post_notification(args.notify_url, title, report):
        ui.warn(f"Could not reach {args.notify_url} to send the notification.")
    return 0 if result.status == "done" else 1
