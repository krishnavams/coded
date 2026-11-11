"""Verification: don't let a turn claim success it hasn't earned.

Two gates run after a turn that changed something, before the answer reaches
the user:

1. **The project's own checks** — whatever command you name (`pytest -q`,
   `npm test`, `make check`). Deterministic, and it helps every model.
2. **A second opinion** — a *different* model reads the request and the diff
   and answers PASS or NEEDS_CHANGES. One model marking its own homework is a
   known weak spot; a reviewer that didn't write the code is the cheapest
   correction available when several endpoints are configured.

Either gate's complaint goes back to the agent as feedback, for a bounded
number of rounds. Both are off until configured: `verify.command` turns on the
checks, and the second opinion needs `model_roles.verify` set, so a 7B model is
never promoted to critic by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PASS = "PASS"
NEEDS_CHANGES = "NEEDS_CHANGES"
UNKNOWN = "UNKNOWN"

_DIFF_LIMIT = 24_000
_OUTPUT_LIMIT = 6_000


@dataclass
class VerifySettings:
    """`"verify": {...}` in config.json."""

    enabled: bool = True
    command: Optional[str] = None       # the project's own checks
    timeout: float = 300.0
    max_rounds: int = 2                 # how many times a turn may be sent back
    second_opinion: bool = False        # needs model_roles.verify

    @classmethod
    def from_config(cls, data: Optional[Dict[str, Any]]) -> "VerifySettings":
        data = data or {}
        try:
            timeout = float(data.get("timeout", 300.0))
        except (TypeError, ValueError):
            timeout = 300.0
        try:
            rounds = int(data.get("max_rounds", 2))
        except (TypeError, ValueError):
            rounds = 2
        return cls(
            enabled=bool(data.get("enabled", True)),
            command=(data.get("command") or None),
            timeout=timeout,
            max_rounds=max(0, rounds),
            second_opinion=bool(data.get("second_opinion", False)),
        )

    def runs_checks(self) -> bool:
        return bool(self.enabled and self.command and self.max_rounds)

    def runs_review(self, has_verify_model: bool) -> bool:
        return bool(self.enabled and self.second_opinion and has_verify_model
                    and self.max_rounds)


@dataclass
class CheckResult:
    ok: bool
    output: str
    command: str
    error: str = ""  # the check could not be run at all


def run_checks(command: str, cwd: str, timeout: float, limit: int = _OUTPUT_LIMIT
               ) -> CheckResult:
    """Run the project's own check command. Its exit status is the verdict."""
    from coded.shells import run

    try:
        proc = run(command, cwd=cwd, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a timeout or a missing shell
        return CheckResult(False, "", command, error=f"{type(exc).__name__}: {exc}")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if len(out) > limit:
        # Keep the tail: that is where the failures and the summary are.
        out = "… (earlier output trimmed) …\n" + out[-limit:]
    return CheckResult(proc.returncode == 0, out, command)


# ---------------------------------------------------------------------------
# The diff of what this turn changed
# ---------------------------------------------------------------------------
def turn_diff(checkpoints, cwd: str, limit: int = _DIFF_LIMIT) -> Tuple[str, List[str]]:
    """A unified diff of the files this turn touched, from the checkpoint backups.

    Returns (diff, paths). `bash` changes are deliberately not checkpointed, so
    this can be incomplete — the caller says so when telling a reviewer.
    """
    import difflib

    group = getattr(checkpoints, "current", None)
    if group is None or not getattr(group, "records", None):
        return "", []
    pieces: List[str] = []
    paths: List[str] = []
    for record in group.records.values():
        path = Path(record["path"])
        try:
            rel = str(path.relative_to(Path(cwd)))
        except (ValueError, TypeError):
            rel = str(path)
        paths.append(rel)
        before = ""
        if record.get("existed") and record.get("backup"):
            before = _read(group.directory / record["backup"])
        after = _read(path)
        if before == after:
            continue
        diff = difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                    fromfile=f"a/{rel}", tofile=f"b/{rel}")
        pieces.append("".join(diff))
    text = "\n".join(p for p in pieces if p)
    if len(text) > limit:
        text = text[:limit] + "\n… (diff truncated) …"
    return text, paths


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# The second opinion
# ---------------------------------------------------------------------------
_REVIEW_SYSTEM = (
    "You are reviewing another engineer's change before it is handed to the user. "
    "You did not write it. Judge only whether it does what was asked and whether it "
    "is correct: wrong logic, a case the change breaks, something claimed but not "
    "done, an obvious security or data-loss problem. Style, naming and taste are "
    "not your business. Say NEEDS_CHANGES only for something you can point at in "
    "the diff."
)

_REVIEW_FORMAT = (
    "Respond with EXACTLY this format:\n"
    "First line: 'VERDICT: PASS' or 'VERDICT: NEEDS_CHANGES'.\n"
    "If NEEDS_CHANGES, list each problem on its own line: the file, what is wrong, "
    "and what to do about it. Be specific and brief. If the change is fine, say "
    "PASS and nothing else."
)


def second_opinion(agent, request: str, diff: str, paths: List[str],
                   bash_ran: bool, checks: Optional[CheckResult] = None) -> Tuple[str, str]:
    """Ask the `verify` role model to review this turn. Returns (verdict, notes)."""
    model = agent.config.model_for_role("verify", agent.model)
    parts = [f"The user asked:\n{request.strip()[:4000]}\n"]
    if paths:
        parts.append("Files this turn touched: " + ", ".join(sorted(paths)[:40]))
    if bash_ran:
        parts.append("Note: the turn also ran shell commands, whose edits are not in "
                     "the diff below. Judge what you can see.")
    parts.append("Diff:\n" + (diff or "(no file diff captured)"))
    if checks is not None and checks.output:
        parts.append(f"The project's checks (`{checks.command}`) "
                     f"{'passed' if checks.ok else 'FAILED'}:\n{checks.output[-2000:]}")
    parts.append(_REVIEW_FORMAT)
    messages = [{"role": "system", "content": _REVIEW_SYSTEM},
                {"role": "user", "content": "\n\n".join(parts)}]
    try:
        llm = agent.llm if model is agent.model else agent._make_llm(model)
        completion = llm.complete(messages, tools=None, stream=False)
    except Exception as exc:  # noqa: BLE001 - a reviewer outage must not fail the turn
        return UNKNOWN, f"(second opinion unavailable: {exc})"
    text = (completion.content or "").strip()
    head = text.upper()[:120]
    if NEEDS_CHANGES in head:
        return NEEDS_CHANGES, text
    if PASS in head:
        return PASS, text
    return UNKNOWN, text  # unparseable: no feedback, no extra round


# ---------------------------------------------------------------------------
# What the agent is told
# ---------------------------------------------------------------------------
def checks_feedback(result: CheckResult) -> str:
    if result.error:
        return (f"[verify] The project's checks (`{result.command}`) could not be run: "
                f"{result.error}\nSay so in your answer; do not claim the change is "
                "verified.")
    return (
        f"[verify] You are not done: `{result.command}` failed.\n\n{result.output}\n\n"
        "Fix the cause. Never weaken, skip or delete a test to make this pass. "
        "If the failure is unrelated to your change — it was already failing before "
        "you started — say so plainly in your answer and stop rather than fixing it."
    )


def review_feedback(notes: str) -> str:
    return (
        "[verify] A second model reviewed your change and asked for fixes:\n\n"
        f"{notes.strip()}\n\n"
        "Apply what is right and say so. If a point is wrong, say why in your answer "
        "instead of changing the code — the reviewer did not write this and may be "
        "mistaken."
    )


def exhausted_note(rounds: int, what: str) -> str:
    return (f"Verification did not pass after {rounds} round(s): {what}. "
            "The answer above is unverified.")
