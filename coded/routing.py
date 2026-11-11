"""Choosing which model answers a request: difficulty routing and model pools.

Both are off until configured, and both only ever pick between models the user
has already defined.

**Routing** (``"routing"`` in config.json) looks at the request itself and picks
a target for the turn::

    {"routing": {"enabled": true, "hard": "large", "medium": "medium",
                 "simple": "small",
                 "rules": [{"match": "(?i)^deploy\\\\b", "level": "hard"}]}}

The classifier is a heuristic over the raw request, not a model call, so it is
free and deterministic. It only moves off the user's model when the evidence is
clear: a request it cannot read ("fix it", "yes") keeps the model from the
previous request, which is what a follow-up means. A level is never applied if
the conversation would not fit the smaller model's context window, and an
explicit ``-m``/``/model`` pins the model and turns routing off.

**Pools** (``"pools"`` in config.json) are several interchangeable models behind
one name::

    {"pools": {"big": ["large-a", "large-b"]}}

A pool name can be used anywhere a model alias can (``-m big``,
``default_model``, ``model_roles``, routing targets). One member is picked when
a pool is resolved — at session start, per sub-agent, per routed turn — and the
agent then stays on it, so the endpoint keeps its prompt cache. The other
members become its fallbacks, and a member that fails is skipped for everyone
(endpoint health is process-wide, see ``llm.endpoint_for``).
"""

from __future__ import annotations

import itertools
import random
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

LEVELS = ("simple", "medium", "hard")

# Positive evidence. Nothing here fires on a bare follow-up like "fix it".
_HARD = re.compile(
    r"\b(architect\w*|design\s+(a|the|an)|refactor\w*|redesign|migrat\w+|rewrite|"
    r"debug|root\s+cause|race\s+condition|deadlock|concurren\w+|thread[- ]safe|"
    r"memory\s+leak|optimi[sz]\w+|performance|profil\w+|benchmark|"
    r"security|vulnerab\w+|injection|auth\w*\s+(flow|system)|"
    r"across\s+(the\s+)?(code\s*base|repo\w*|project)|end[- ]to[- ]end|"
    r"why\s+(does|is|are|did)|investigate|trace\s+down)\b", re.I)
# "fix" on its own is not evidence of anything ("fix it" is a follow-up, "fix the
# typo" is trivial), so it is deliberately absent here.
_MEDIUM = re.compile(
    r"\b(implement|add\s+(a|an|the|support|tests?)|write\s+(a|an|the|tests?)|build|"
    r"create|handle|extend|wire\s+up|hook\s+up|endpoint|feature)\b", re.I)
_SIMPLE = re.compile(
    r"\b(typo|rename|spelling|format(ting)?|reformat|comment|docstring|"
    r"bump\s+(the\s+)?version|what\s+(is|does|are)|where\s+(is|are)|list\s+|"
    r"show\s+me|print|read\s+the|lint)\b", re.I)

_LONG_IS_HARD = 900  # chars: a long brief is a big task whatever words it uses


@dataclass
class RoutingSettings:
    enabled: bool = False
    simple: Optional[str] = None
    medium: Optional[str] = None
    hard: Optional[str] = None
    # [{"match": "<regex>", "level": "hard"}] — tried first, in order.
    rules: List[Dict[str, Any]] = field(default_factory=list)
    # Once a session has gone up a level, don't drop back down.
    sticky: bool = True

    @classmethod
    def from_config(cls, data: Optional[Dict[str, Any]]) -> "RoutingSettings":
        data = data or {}
        rules = [r for r in (data.get("rules") or []) if isinstance(r, dict)]
        return cls(
            enabled=bool(data.get("enabled", False)),
            simple=data.get("simple"), medium=data.get("medium"), hard=data.get("hard"),
            rules=rules, sticky=bool(data.get("sticky", True)),
        )

    def target(self, level: Optional[str]) -> Optional[str]:
        return getattr(self, level, None) if level in LEVELS else None

    @property
    def configured(self) -> bool:
        return self.enabled and any(getattr(self, lvl) for lvl in LEVELS)


def classify(message: str, settings: Optional[RoutingSettings] = None) -> Optional[str]:
    """How hard this request looks: "simple", "medium", "hard", or None.

    None means "can't tell" — the caller keeps the model it already has, because
    a short follow-up continues the work the previous request started.
    """
    text = (message or "").strip()
    if not text:
        return None
    for rule in (settings.rules if settings else []):
        pattern, level = rule.get("match"), rule.get("level")
        if not pattern or level not in LEVELS:
            continue
        try:
            if re.search(pattern, text):
                return level
        except re.error:
            continue
    if len(text) >= _LONG_IS_HARD or _HARD.search(text):
        return "hard"
    simple, medium = _SIMPLE.search(text), _MEDIUM.search(text)
    if simple and "```" not in text:
        return "simple"
    if medium:
        return "medium"
    # No evidence either way: "fix it", "yes", "now the tests" continue whatever
    # the last request started, so the caller keeps its model.
    return None


def harder(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """The higher of two levels (None is lower than any level)."""
    order = {None: -1, "simple": 0, "medium": 1, "hard": 2}
    return a if order.get(a, -1) >= order.get(b, -1) else b


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------
_counters: Dict[str, Any] = {}
_lock = threading.Lock()
_seed = random.Random()


def _counter(name: str):
    with _lock:
        counter = _counters.get(name)
        if counter is None:
            # Start somewhere random so every process doesn't hammer member 0.
            counter = itertools.count(_seed.randrange(0, 1000))
            _counters[name] = counter
        return counter


def reset_pools(seed: Optional[int] = None) -> None:
    """Forget rotation state (tests). With `seed`, rotation becomes repeatable."""
    global _seed
    with _lock:
        _counters.clear()
    if seed is not None:
        _seed = random.Random(seed)


def pick_member(name: str, members: Sequence[str],
                healthy: Optional[Sequence[str]] = None) -> Optional[str]:
    """The next member of pool `name`, round-robin, preferring healthy ones."""
    members = [m for m in members if m]
    if not members:
        return None
    usable = [m for m in members if m in healthy] if healthy is not None else members
    pool = usable or members
    return pool[next(_counter(name)) % len(pool)]


def healthy_members(members: Sequence[str], models: Dict[str, Any]) -> List[str]:
    """Members whose endpoint is not currently cooling down after a failure."""
    from coded.llm import endpoint_for

    out = []
    for alias in members:
        model = models.get(alias)
        if model is None:
            continue
        try:
            import time

            if endpoint_for(model).down_until <= time.monotonic():
                out.append(alias)
        except Exception:  # noqa: BLE001 - health is best-effort
            out.append(alias)
    return out
