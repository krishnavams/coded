"""Self-improvement lab: learn from this project's past sessions.

`coded lab` (and `/lab`) read the saved sessions of this folder — newest
first, capped by count and size — and their event logs, and look for what
keeps going wrong: the same tool error again and again, actions the user keeps
denying, checks that stall, turns that hit max_turns, the agent repeating the
identical call, model fallbacks churning. Plain heuristics, no model call.

Each finding can carry a proposal — a project memory note or a setting —
shown as the exact change. Nothing is applied
without the user: `coded lab --apply` (or `/lab apply`) asks about each one
separately, only at a terminal (there is no --yes), and a proposal that would
loosen what the agent may do is flagged and never batched with the others.
Changes go only to this project's memory (`.coded/memory.json`, not shared
through git) or its `.coded/config.json`, never to coded's own files or to
committed instruction files.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_SESSIONS = 40
MAX_SESSION_BYTES = 8_000_000
_MIN_REPEATS = 3


@dataclass
class Proposal:
    kind: str            # "memory" | "setting"
    value: Any
    preview: str         # the exact change, as shown before applying
    loosens: bool = False


@dataclass
class Finding:
    title: str
    evidence: str
    count: int
    proposal: Optional[Proposal] = None


@dataclass
class Report:
    sessions: int
    turns: int
    tool_calls: int
    findings: List[Finding] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading sessions
# ---------------------------------------------------------------------------
def load_sessions(cwd: str, store=None, limit: int = MAX_SESSIONS) -> List[Dict[str, Any]]:
    """This folder's saved sessions (raw JSON), newest first, bounded."""
    from coded.sessions_store import SessionStore

    store = store or SessionStore()
    cwd = os.path.realpath(cwd)
    out = []
    for meta in store.list():
        if os.path.realpath(meta.cwd or "") != cwd:
            continue
        path = store.dir / f"{meta.id}.json"
        try:
            if path.stat().st_size > MAX_SESSION_BYTES:
                continue
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if len(out) >= limit:
            break
    return out


def _normalize(text: str) -> str:
    """An error message with its specifics removed, so repeats group together."""
    text = re.sub(r"(/|[A-Za-z]:\\)[^\s'\"]+", "<path>", text or "")
    text = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "<x>", text)
    text = re.sub(r"\d+", "N", text)
    return " ".join(text.split())[:140]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyze(sessions: List[Dict[str, Any]]) -> Report:
    events = [(i, e) for i, s in enumerate(sessions) for e in (s.get("events") or [])
              if isinstance(e, dict)]
    tools = [(i, e) for i, e in events if e.get("kind") == "tool"]
    report = Report(sessions=len(sessions),
                    turns=sum(1 for _, e in events if e.get("kind") == "turn"),
                    tool_calls=len(tools))

    # 1. The same tool error, again and again.
    errors: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    examples: Dict[Tuple[str, str], str] = {}
    for i, e in tools:
        if e.get("error") and not e.get("denied"):
            key = (str(e.get("name")), _normalize(str(e.get("detail", ""))))
            errors[key].append(i)
            examples.setdefault(key, str(e.get("detail", "")))
    for (tool, norm), hits in sorted(errors.items(), key=lambda kv: -len(kv[1])):
        if len(hits) < _MIN_REPEATS:
            continue
        example = examples[(tool, norm)][:160]
        line = (f"The `{tool}` tool often fails with: {norm}. Check the cause before "
                f"calling it again instead of retrying the same call.")
        report.findings.append(Finding(
            f"`{tool}` keeps failing the same way", f"{len(hits)} times in "
            f"{len(set(hits))} session(s), e.g. “{example}”", len(hits),
            Proposal("memory", line, f"+ (gotcha) {line}   (project memory, .coded/memory.json)")))

    # 2. Actions the user keeps saying no to.
    # Only what the user turned down: plan mode, hooks, deny rules and "nobody
    # to ask" refusals say nothing about the user's preferences.
    denied = Counter(str(e.get("name")) for _, e in tools
                     if e.get("denied") and e.get("reason") == "user")
    for tool, n in denied.most_common():
        if n < _MIN_REPEATS:
            continue
        line = (f"The user usually declines `{tool}` in this project: ask in your answer "
                "before planning around it.")
        report.findings.append(Finding(
            f"You denied `{tool}` {n} times", "the agent keeps proposing an action you "
            "turn down", n, Proposal("memory", line,
                                     f"+ (gotcha) {line}   (project memory, .coded/memory.json)")))

    # 3. Checks that stall.
    stalls = [e for _, e in events if e.get("kind") == "verify" and e.get("result") == "stall"]
    if len(stalls) >= 2:
        cmd = stalls[-1].get("command", "")
        report.findings.append(Finding(
            "The checks fail the same way after a fix", f"{len(stalls)} stalled rounds "
            f"(`{cmd}`): often a test that was already failing", len(stalls),
            Proposal("memory", f"`{cmd}` has failures that predate recent changes; check "
                     "which tests fail before starting a fix.",
                     f"+ (gotcha) `{cmd}` has failures that predate recent changes…   "
                     "(project memory, .coded/memory.json)")))

    # 4. Turns that ran out of steps.
    caps = [e for _, e in events if e.get("kind") == "max_turns"]
    if caps:
        limit = int(caps[-1].get("limit") or 100)
        report.findings.append(Finding(
            "Turns hit max_turns", f"{len(caps)} turn(s) stopped at {limit} steps", len(caps),
            Proposal("setting", {"max_turns": limit * 2},
                     f"max_turns: {limit} → {limit * 2}   (in .coded/config.json)", loosens=True)))

    # 5. The identical call repeated within one session.
    for i, s in enumerate(sessions):
        calls = Counter()
        for m in s.get("messages") or []:
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                calls[(fn.get("name"), fn.get("arguments"))] += 1
        for (name, args), n in calls.items():
            if n >= _MIN_REPEATS + 1 and name not in ("todo_write",):
                report.findings.append(Finding(
                    f"The agent repeated the same `{name}` call {n} times",
                    f"in one session, with arguments {str(args)[:100]}", n))
                break

    # 6. Fallback churn.
    falls = [e for _, e in events if e.get("kind") == "note" and
             re.search(r"fall(ing)? ?back|fell back", str(e.get("text", "")), re.I)]
    if len(falls) >= 5:
        report.findings.append(Finding(
            "Models keep falling back", f"{len(falls)} fallbacks, e.g. "
            f"“{str(falls[-1].get('text', ''))[:120]}”", len(falls)))
    report.findings.sort(key=lambda f: -f.count)
    return report


# ---------------------------------------------------------------------------
# Rendering and applying
# ---------------------------------------------------------------------------
def render(report: Report):
    from rich.box import ROUNDED
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    from coded import ui

    t = Text()
    t.append(f"{report.sessions} session(s) · {report.turns} request(s) · "
             f"{report.tool_calls} tool call(s) read\n", style=ui.MUTED)
    if not report.sessions:
        t.append("\nNo saved sessions for this folder yet.", style=ui.MUTED)
    elif not report.findings:
        t.append("\nNothing recurring stands out. ", style=ui.ACCENT2)
        t.append("(Sessions saved before the event log existed carry no outcomes.)",
                 style=ui.MUTED)
    for n, f in enumerate(report.findings, 1):
        t.append(f"\n{n}. {f.title}\n", style="bold")
        t.append(f"   {f.evidence}\n", style=ui.MUTED)
        if f.proposal:
            style = ui.WARN if f.proposal.loosens else ui.ACCENT2
            t.append("   proposal: ", style=ui.MUTED)
            t.append(f.proposal.preview + "\n", style=style)
            if f.proposal.loosens:
                t.append("   ⚠ this lets the agent do more; it is asked about on its own\n",
                         style=ui.WARN)
    foot = Text("Nothing changes until you run `coded lab --apply` (or /lab apply) and say yes "
                "to each proposal.", style=ui.MUTED)
    return Group(Panel(t, title=f"[bold {ui.ACCENT}]Self-improvement lab[/]",
                       title_align="left", border_style=ui.ACCENT, box=ROUNDED), foot)


def apply_proposal(cwd: str, p: Proposal) -> str:
    """Make one approved change. Only this project's files are touched."""
    if p.kind == "memory":
        from coded.memory import MemoryStore

        entry = MemoryStore.for_project(cwd).add(p.value, "gotcha", source="user")
        return "saved as a project note" if entry else "already a project note"
    if p.kind == "setting":
        from coded.config import project_config_path
        from coded.config_wizard import read_file, write_file

        path = project_config_path(cwd)
        data = read_file(path)
        data.update(p.value)
        write_file(path, data)
        return f"written to {path}"
    return "unknown proposal kind; nothing changed"


def apply_interactive(cwd: str, report: Report, ask: Callable[[str], str],
                      say: Callable[[str], None]) -> int:
    """Ask about each proposal on its own; loosening ones get an extra warning.
    Returns how many were applied."""
    applied = 0
    for f in report.findings:
        p = f.proposal
        if p is None:
            continue
        say(f"\n{f.title}\n  {p.preview}")
        if p.loosens:
            say("  ⚠ This lets the agent do more than it can now.")
        answer = (ask("  Apply this change? [y/N] ") or "").strip().lower()
        if answer in ("y", "yes"):
            say("  " + apply_proposal(cwd, p))
            applied += 1
        else:
            say("  skipped")
    return applied
