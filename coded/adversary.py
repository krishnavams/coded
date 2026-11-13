"""Adversarial review: an independent agent that tries to break a change.

`/redteam [target]` (REPL, TUI) and `coded review --adversarial` run the
built-in `adversary` skill in a **read-only** sub-agent — built without any
file-changing tool, not merely told to keep its hands off — with a fresh
context that holds only the target, never the author's transcript. It runs on
`model_roles.review` when set; when that is the same model that wrote the
code, the report says so (one model marking its own homework is weaker).

Every finding must name a real file and line, a concrete trigger and a
severity; anything else is dropped before the user sees it, and the report
says how many were dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

SEVERITIES = ("high", "medium", "low")
_LOCATION = re.compile(r"^(?P<path>[^\s:][^:]*?):(?P<line>\d+)(?:[-:]\d+)?$")
_DEFAULT_TARGET = ("the current uncommitted changes — inspect them with the git tool "
                   "(`diff` and `diff --cached`)")


@dataclass
class Finding:
    severity: str
    location: str
    claim: str
    trigger: str
    impact: str = ""


def parse(report: str, cwd: str) -> Tuple[List[Finding], int]:
    """(findings that carry their evidence, how many were dropped)."""
    blocks = re.split(r"(?mi)^\s*FINDING\s*$", report or "")[1:]
    kept: List[Finding] = []
    dropped = 0
    for block in blocks:
        fields = {}
        for line in block.splitlines():
            m = re.match(r"\s*(severity|location|claim|trigger|impact)\s*:\s*(.+)", line, re.I)
            if m:
                fields[m.group(1).lower()] = m.group(2).strip().strip("`")
        sev = fields.get("severity", "").lower().split()[0] if fields.get("severity") else ""
        loc = _LOCATION.match(fields.get("location", ""))
        ok = (sev in SEVERITIES and loc and fields.get("claim") and fields.get("trigger")
              and len(fields["trigger"]) >= 8)
        if ok:
            path = Path(loc.group("path"))
            full = path if path.is_absolute() else Path(cwd) / path
            ok = full.is_file()  # a finding about a file that does not exist is a guess
        if not ok:
            dropped += 1
            continue
        kept.append(Finding(sev, fields["location"], fields["claim"], fields["trigger"],
                            fields.get("impact", "")))
    kept.sort(key=lambda f: SEVERITIES.index(f.severity))
    return kept, dropped


def render(findings: List[Finding], dropped: int, model_note: str, raw: str) -> str:
    """The report, in Markdown."""
    lines = ["# Adversarial review", "", f"_{model_note}_", ""]
    if not findings:
        said_none = "NO FINDINGS" in (raw or "").upper()
        lines.append("No findings with evidence." if said_none or not raw else
                     "No finding came with a file:line, a concrete trigger and a severity.")
    for i, f in enumerate(findings, 1):
        lines += [f"**{i}. [{f.severity.upper()}] `{f.location}`** — {f.claim}",
                  f"- trigger: {f.trigger}"]
        if f.impact:
            lines.append(f"- impact: {f.impact}")
        lines.append("")
    if dropped:
        lines.append(f"_{dropped} claim(s) dropped: no real file:line, no concrete trigger or "
                     "no severity._")
    return "\n".join(lines).rstrip() + "\n"


def run(agent, target: Optional[str] = None) -> str:
    """Run the adversary on `target` and return the validated report."""
    skill = (getattr(agent, "skills", None) or {}).get("adversary")
    if skill is None:
        return "The adversary skill is not available (skills may be disabled with --no-skills).\n"
    reviewer = agent.config.model_for_role("review", agent.model)
    if reviewer.name == agent.model.name:
        model_note = (f"Reviewed by {reviewer.name}, the same model as the main agent — set "
                      "model_roles.review to a different model for a more independent review.")
    else:
        model_note = f"Reviewed by {reviewer.name} (independent of the main model, {agent.model.name})."
    prompt = (
        "You are an independent adversarial reviewer. You have not seen how this change "
        "was made, only the code.\n\n" + skill.load_body(reviewer) +
        f"\n\n## Target\n{target or _DEFAULT_TARGET}\n"
    )
    try:
        raw = agent.run_subagent(prompt, role="review", read_only=True)
    except Exception as exc:  # noqa: BLE001
        return f"# Adversarial review\n\n(the review failed: {exc})\n"
    findings, dropped = parse(raw, agent.cwd)
    return render(findings, dropped, model_note, raw)
