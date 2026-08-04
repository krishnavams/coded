"""Permission / confirmation layer for potentially dangerous tool actions.

Tools that mutate the filesystem or run shell commands declare
``requires_permission = True``. Before such a tool runs, the agent asks the
PermissionManager, which may prompt the user. The user can approve once, deny,
or approve all future actions of that kind for the session.
"""

from __future__ import annotations

import fnmatch
from enum import Enum
from typing import Callable, List, Optional, Set


class Decision(Enum):
    ALLOW = "allow"
    DENY = "deny"


# A prompt callback returns one of: "yes", "no", "always".
PromptFn = Callable[[str, str], str]


def _parse_rule(pattern: str):
    """"bash" -> ("bash", None); "bash(git *)" -> ("bash", "git *")."""
    pattern = pattern.strip()
    if pattern.endswith(")") and "(" in pattern:
        name, inner = pattern[:pattern.index("(")], pattern[pattern.index("(") + 1:-1]
        return name.strip(), inner.strip()
    return pattern, None


def _rule_matches(pattern: str, key: str, target: str) -> bool:
    name, glob = _parse_rule(pattern)
    if name not in (key, "*"):
        return False
    if glob is None:
        return True
    return fnmatch.fnmatch(target or "", glob)


class PermissionManager:
    def __init__(self, auto_approve: bool = False, prompt: Optional[PromptFn] = None,
                 rules: Optional[dict] = None):
        # auto_approve == "yolo" mode: never ask, always allow (deny rules still win).
        self.auto_approve = auto_approve
        self._prompt = prompt
        self._always: Set[str] = set()
        rules = rules or {}
        self.allow_rules: List[str] = list(rules.get("allow") or [])
        self.deny_rules: List[str] = list(rules.get("deny") or [])

    def set_prompt(self, prompt: Optional[PromptFn]) -> None:
        self._prompt = prompt

    def allow_always(self, key: str) -> None:
        self._always.add(key)

    def _matches(self, patterns: List[str], key: str, target: str) -> bool:
        return any(_rule_matches(p, key, target) for p in patterns)

    def request(self, *, key: str, title: str, detail: str, target: Optional[str] = None) -> Decision:
        """Ask whether an action is permitted.

        key    — a stable identifier (usually the tool name), matched by rules.
        title  — short human label (e.g. "Run shell command").
        detail — the specifics shown in the prompt (command or file path).
        target — the matchable string for allow/deny rules (defaults to detail).
        """
        target = target if target is not None else detail
        # Deny rules win over everything, including --yolo.
        if self._matches(self.deny_rules, key, target):
            return Decision.DENY
        if self.auto_approve:
            return Decision.ALLOW
        if key in self._always:
            return Decision.ALLOW
        if self._matches(self.allow_rules, key, target):
            return Decision.ALLOW
        if self._prompt is None:
            # Non-interactive and not auto-approved: deny by default (safe).
            return Decision.DENY
        answer = self._prompt(title, detail).strip().lower()
        if answer in ("a", "always"):
            self._always.add(key)
            return Decision.ALLOW
        if answer in ("y", "yes"):
            return Decision.ALLOW
        return Decision.DENY
