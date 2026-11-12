"""Permission / confirmation layer for potentially dangerous tool actions.

Tools that mutate the filesystem or run shell commands declare
``requires_permission = True``. Before such a tool runs, the agent asks the
PermissionManager, which may prompt the user. The user can approve once, deny,
or approve all future actions of that kind for the session.
"""

from __future__ import annotations

import fnmatch
import threading
from enum import Enum
from typing import Callable, List, Optional, Set


# File-editing tools that "accept edits" mode lets through without asking.
# Deleting files and running commands still ask.
EDIT_KEYS = frozenset({"write", "edit", "multi_edit", "apply_patch", "move", "notebook_edit"})


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
                 rules: Optional[dict] = None, injection_guard: str = "warn"):
        # auto_approve == "yolo" mode: never ask, always allow (deny rules still win).
        self.auto_approve = auto_approve
        self._prompt = prompt
        self._prompt_threadsafe = False
        self._question: Optional[Callable[[str, List[str]], Optional[str]]] = None
        self._lock = threading.Lock()
        self._always: Set[str] = set()
        rules = rules or {}
        self.allow_rules: List[str] = list(rules.get("allow") or [])
        self.deny_rules: List[str] = list(rules.get("deny") or [])
        # Plan mode: every risky action is refused (read-only research).
        self.plan_mode = False
        # Accept-edits mode: file edits (EDIT_KEYS) run without asking.
        self.accept_edits = False
        # Set once untrusted content (web pages, PR bodies, MCP output) entered the
        # context this turn. "strict" then ignores auto-approval; "warn" flags prompts.
        self.injection_guard = injection_guard
        self.tainted = False

    def set_prompt(self, prompt: Optional[PromptFn], threadsafe: bool = False) -> None:
        self._prompt = prompt
        self._prompt_threadsafe = threadsafe

    @property
    def interactive(self) -> bool:
        return self._prompt is not None

    def can_prompt_concurrently(self) -> bool:
        """Whether tools that may prompt can safely run on worker threads."""
        return self._prompt is None or self._prompt_threadsafe or self.auto_approve

    def ask(self, title: str, detail: str) -> Optional[str]:
        """Ask the user a yes/no question directly. None when non-interactive."""
        if self._prompt is None:
            return None
        with self._lock:
            return self._prompt(title, detail)

    def set_question(self, fn: Optional[Callable[[str, List[str]], Optional[str]]]) -> None:
        """The front-end's way to ask the user a free-form question (ask_user)."""
        self._question = fn

    def question(self, text: str, options: Optional[List[str]] = None) -> Optional[str]:
        """Ask the user a question; their answer, or None when nobody can answer."""
        fn = getattr(self, "_question", None)
        if fn is None:
            return None
        with self._lock:
            return fn(text, list(options or []))

    def allow_always(self, key: str) -> None:
        self._always.add(key)

    @property
    def always_allowed(self) -> List[str]:
        """Tools approved with "always" this session."""
        return sorted(self._always)

    def add_rule(self, kind: str, rule: str) -> bool:
        """Add an allow/deny rule for this session; False if already there."""
        rules = self.allow_rules if kind == "allow" else self.deny_rules
        rule = rule.strip()
        if not rule or rule in rules:
            return False
        rules.append(rule)
        return True

    def remove_rule(self, rule: str) -> bool:
        """Remove a rule from either list (or a session "always" approval)."""
        rule = rule.strip()
        found = False
        for rules in (self.allow_rules, self.deny_rules):
            if rule in rules:
                rules.remove(rule)
                found = True
        if rule in self._always:
            self._always.discard(rule)
            found = True
        return found

    def _matches(self, patterns: List[str], key: str, target: str) -> bool:
        return any(_rule_matches(p, key, target) for p in patterns)

    def request(self, *, key: str, title: str, detail: str, target: Optional[str] = None,
                auto_allowed: bool = False) -> Decision:
        """Ask whether an action is permitted.

        key          — a stable identifier (usually the tool name), matched by rules.
        title        — short human label (e.g. "Run shell command").
        detail       — the specifics shown in the prompt (command or file path).
        target       — the matchable string for allow/deny rules (defaults to detail).
        auto_allowed — the tool vouches the call is safe (e.g. it runs sandboxed).
        """
        target = target if target is not None else detail
        # Deny rules win over everything, including --yolo.
        if self._matches(self.deny_rules, key, target):
            return Decision.DENY
        if self.plan_mode:
            return Decision.DENY
        strict = self.tainted and self.injection_guard == "strict"
        if not strict:
            if auto_allowed or self.auto_approve or key in self._always:
                return Decision.ALLOW
            if self.accept_edits and key in EDIT_KEYS:
                return Decision.ALLOW
            if self._matches(self.allow_rules, key, target):
                return Decision.ALLOW
        if self._prompt is None:
            # Non-interactive and not auto-approved: deny by default (safe).
            return Decision.DENY
        if self.tainted and self.injection_guard != "off":
            title = f"⚠ untrusted content is in context — {title}"
        with self._lock:
            answer = self._prompt(title, detail).strip().lower()
        if answer in ("a", "always"):
            self._always.add(key)
            return Decision.ALLOW
        if answer in ("y", "yes"):
            return Decision.ALLOW
        return Decision.DENY
