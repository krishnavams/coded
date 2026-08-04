"""Multi-role review pipeline: run reviewer → qa → security as sub-agents.

Each phase loads a built-in role skill and runs it in an isolated sub-agent, so
the phases don't pollute each other's (or the main) context. Reports are printed
as they complete and returned aggregated.
"""

from __future__ import annotations

from typing import List, Optional

from coded import ui

# Ordered pipeline. Each name must match a discovered skill.
REVIEW_PHASES = ["reviewer", "qa", "security"]

# Per-phase framing so a review stays focused and mostly non-mutating.
_PHASE_FRAMING = {
    "reviewer": "Review the target for correctness, bugs, edge cases, and quality.",
    "qa": ("Assess the test coverage of the target and run the existing test suite to "
           "see if it passes. Report gaps. Do NOT write new test files unless one is "
           "trivial and clearly missing."),
    "security": "Audit the target for security vulnerabilities.",
}

_DEFAULT_TARGET = ("the current uncommitted changes — inspect them with the git tool "
                   "(`diff` and `diff --cached`) before reviewing")


def run_review(agent, target: Optional[str] = None,
               phases: Optional[List[str]] = None, verbose: bool = True) -> str:
    """Run the review pipeline and return an aggregated Markdown report."""
    target = target or _DEFAULT_TARGET
    phases = phases or REVIEW_PHASES

    if not getattr(agent, "skills", None):
        return ("No role skills are available (skills may be disabled with --no-skills). "
                "Cannot run the review pipeline.")

    sections: List[tuple] = []
    for name in phases:
        skill = agent.skills.get(name)
        if skill is None:
            sections.append((name, f"(skipped: no '{name}' skill found)"))
            continue
        if verbose:
            ui.rule(f"{name} phase")
        body = skill.load_body()
        framing = _PHASE_FRAMING.get(name, "")
        prompt = (
            "You are one phase of an automated code-review pipeline. Play the role "
            "described below and report concisely; another agent handles the other "
            f"phases.\n\n{body}\n\n## Review target\n{target}\n\n{framing}\n"
            "Return only your findings."
        )
        try:
            report = agent.run_subagent(prompt)
        except Exception as exc:  # noqa: BLE001
            report = f"(phase failed: {exc})"
        sections.append((name, report))
        if verbose:
            ui.assistant_markdown(report)

    out = ["# Review summary", f"Target: {target}", ""]
    for name, report in sections:
        out.append(f"## {name}\n\n{report}\n")
    aggregated = "\n".join(out)
    if verbose:
        ui.rule("review complete")
    return aggregated
