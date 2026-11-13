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


# A prompt callback returns one of: "yes", "no", "always", or "no: <feedback>"
# (the user said no and what to do instead).
PromptFn = Callable[[str, str], str]
# The front-end's multi-question prompt (ask_user): the answers, one string per
# question ('' = not answered), or None when the user cancelled.
QuestionsFn = Callable[[list], Optional[List[str]]]


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
        self._questions: Optional[QuestionsFn] = None
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
        # Per thread: a predicate saying "this thread is a background task",
        # which must never stop to ask (nobody is watching it).
        self._local = threading.local()

    def set_background(self, predicate: Optional[Callable[[], bool]]) -> None:
        """Mark the calling thread as (maybe) a background task. The predicate is
        checked on each request, so a task moved to the background mid-run
        (Ctrl+B) stops asking from then on."""
        self._local.background = predicate

    def in_background(self) -> bool:
        fn = getattr(self._local, "background", None)
        try:
            return bool(fn and fn())
        except Exception:  # noqa: BLE001
            return False

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
        if self._prompt is None or self.in_background():
            return None
        with self._lock:
            return self._prompt(title, detail)

    def set_question(self, fn: Optional[Callable[[str, List[str]], Optional[str]]]) -> None:
        """The front-end's way to ask the user a free-form question (ask_user)."""
        self._question = fn

    def set_questions(self, fn: Optional[QuestionsFn]) -> None:
        """The front-end's interactive prompt for one or more `choice.Question`s."""
        self._questions = fn

    @property
    def can_question(self) -> bool:
        return (self._questions is not None or self._question is not None) \
            and not self.in_background()

    def questions(self, items: list) -> Optional[List[str]]:
        """Ask several questions (`choice.Question`s) at once. One answer per
        question ('' = skipped); [] when the user cancelled; None when nobody
        can answer (no front-end, or a background task)."""
        if not self.can_question:
            return None
        with self._lock:
            if self._questions is not None:
                answers = self._questions(list(items))
                return [] if answers is None else list(answers)
            out = []
            for q in items:
                answer = self._question(q.text, [o.label for o in q.options])
                out.append(answer or "")
            return out

    def question(self, text: str, options: Optional[List[str]] = None) -> Optional[str]:
        """Ask the user a question; their answer, or None when nobody can answer."""
        if self._questions is not None and not self.in_background():
            from coded.choice import Question

            answers = self.questions([Question.make(text, list(options or []))])
            if answers is None:
                return None
            return answers[0] if answers else ""
        fn = self._question
        if fn is None or self.in_background():
            return None
        with self._lock:
            return fn(text, list(options or []))

    def take_deny_reason(self) -> Optional[str]:
        """Why this thread's last request was denied (once): "user" (they said
        no), "rule" (a deny rule), "plan" (plan mode) or "unattended" (nobody
        could be asked)."""
        reason = getattr(self._local, "deny_reason", None)
        self._local.deny_reason = None
        return reason

    def take_feedback(self) -> str:
        """What the user said with their last "no" on this thread (once)."""
        text = getattr(self._local, "feedback", "") or ""
        self._local.feedback = ""
        return text

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
                auto_allowed: bool = False, always_ask: bool = False) -> Decision:
        """Ask whether an action is permitted.

        key          — a stable identifier (usually the tool name), matched by rules.
        title        — short human label (e.g. "Run shell command").
        detail       — the specifics shown in the prompt (command or file path).
        target       — the matchable string for allow/deny rules (defaults to detail).
        auto_allowed — the tool vouches the call is safe (e.g. it runs sandboxed).
        always_ask   — accept-edits mode and auto_allowed do not skip the prompt
                       (a path outside the project); --yolo, "always" and allow
                       rules still answer it.
        """
        target = target if target is not None else detail
        self._local.feedback = ""
        self._local.deny_reason = None
        # Deny rules win over everything, including --yolo.
        if self._matches(self.deny_rules, key, target):
            self._local.deny_reason = "rule"
            return Decision.DENY
        if self.plan_mode:
            self._local.deny_reason = "plan"
            return Decision.DENY
        strict = self.tainted and self.injection_guard == "strict"
        if not strict:
            if (auto_allowed and not always_ask) or self.auto_approve or key in self._always:
                return Decision.ALLOW
            if self.accept_edits and key in EDIT_KEYS and not always_ask:
                return Decision.ALLOW
            if self._matches(self.allow_rules, key, target):
                return Decision.ALLOW
        if self._prompt is None or self.in_background():
            # Non-interactive (or a background task) and not auto-approved:
            # deny by default (safe).
            self._local.deny_reason = "unattended"
            return Decision.DENY
        if self.tainted and self.injection_guard != "off":
            title = f"⚠ untrusted content is in context — {title}"
        with self._lock:
            raw = (self._prompt(title, detail) or "no").strip()
        answer = raw.lower()
        self._local.deny_reason = "user"
        if answer.startswith("no:"):
            self._local.feedback = raw[3:].strip()
            return Decision.DENY
        if answer in ("a", "always"):
            self._always.add(key)
            return Decision.ALLOW
        if answer in ("y", "yes"):
            return Decision.ALLOW
        return Decision.DENY
