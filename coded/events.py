"""A session's event log: what happened, as structured facts.

The conversation (`session.messages`) is what the model sees, and it has no
room for facts like "this tool call was denied", "took 3.2 s", "the checks
failed" or "the endpoint fell back". Those go here, in `Session.events`, saved
with the session but never sent to an endpoint. The Command Center
(`/dashboard`), the time-travel view (`/timeline`) and the self-improvement lab
(`coded lab`) all read it.

Bounded (the oldest go first), thread-safe (parallel tools and background
sub-agents write concurrently), and a sub-agent writes into its parent's log
with its task id. Every entry has `t` (epoch seconds) and `kind`:

    turn        prompt (first 200 chars)
    tool        name, id, ms, error, denied, reason (user | rule | plan | hook |
                unattended, when denied), outside, task, detail (first line on error)
    verify      result: fail | pass | stall | skipped, command
    note        text (status notes: fallbacks, retries, hooks)
    overflow    a context-window retry
    turns_extended  limit (still making progress at the step limit)
    max_turns   the turn hit max_turns
    interrupt   the user stopped the turn
    answer      chars (the final answer's length)
    llm         model, input, output, cost (one model call; sub-agents too)
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

MAX_EVENTS = 3_000
_DETAIL = 200
# Words a tool result starts with when the user (or the policy) said no.
_DENIED = ("The user denied", "The user did not allow", "Refused:", "Plan mode is on",
           "This tool call was blocked")


def classify(content: str) -> Dict[str, bool]:
    head = (content or "")[:200]
    return {"denied": head.startswith(_DENIED),
            "outside": "outside the project" in head}


class EventLog:
    """Appends to a list it does not own (the session's), under a lock."""

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None, cap: int = MAX_EVENTS):
        self.items: List[Dict[str, Any]] = items if items is not None else []
        self.cap = cap
        self._lock = threading.Lock()

    def add(self, kind: str, **data: Any) -> None:
        entry = {"t": round(time.time(), 3), "kind": kind}
        for key, value in data.items():
            if isinstance(value, str) and len(value) > _DETAIL:
                value = value[:_DETAIL - 1] + "…"
            entry[key] = value
        with self._lock:
            self.items.append(entry)
            if len(self.items) > self.cap:
                del self.items[: len(self.items) - self.cap]

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.items)


def tool_stats(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """name -> calls, errors, denied, ms (total), sorted by calls."""
    stats: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "denied": 0, "ms": 0.0})
    for e in events:
        if e.get("kind") != "tool":
            continue
        s = stats[str(e.get("name", "?"))]
        s["calls"] += 1
        s["errors"] += 1 if e.get("error") and not e.get("denied") else 0
        s["denied"] += 1 if e.get("denied") else 0
        s["ms"] += float(e.get("ms") or 0)
    return dict(sorted(stats.items(), key=lambda kv: -kv[1]["calls"]))


def recent_problems(events: Iterable[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    """The newest errors, denials, failed checks, stalls and stops."""
    out = []
    for e in events:
        kind = e.get("kind")
        if (kind == "tool" and (e.get("error") or e.get("denied"))) or \
                (kind == "verify" and e.get("result") in ("fail", "stall")) or \
                kind in ("max_turns", "interrupt", "overflow"):
            out.append(e)
    return out[-limit:]


def model_usage(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """model -> calls, input, output, cost (sub-agents included)."""
    out: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "input": 0, "output": 0, "cost": 0.0})
    for e in events:
        if e.get("kind") != "llm":
            continue
        m = out[str(e.get("model", "?"))]
        m["calls"] += 1
        m["input"] += int(e.get("input") or 0)
        m["output"] += int(e.get("output") or 0)
        m["cost"] += float(e.get("cost") or 0)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["cost"]))
