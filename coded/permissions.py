"""Permission / confirmation layer for potentially dangerous tool actions.

Tools that mutate the filesystem or run shell commands declare
``requires_permission = True``. Before such a tool runs, the agent asks the
PermissionManager, which may prompt the user. The user can approve once, deny,
or approve all future actions of that kind for the session.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Optional, Set


class Decision(Enum):
    ALLOW = "allow"
    DENY = "deny"


# A prompt callback returns one of: "yes", "no", "always".
PromptFn = Callable[[str, str], str]


class PermissionManager:
    def __init__(self, auto_approve: bool = False, prompt: Optional[PromptFn] = None):
        # auto_approve == "yolo" mode: never ask, always allow.
        self.auto_approve = auto_approve
        self._prompt = prompt
        # Rules the user chose to "always allow", keyed by a stable signature.
        self._always: Set[str] = set()

    def set_prompt(self, prompt: Optional[PromptFn]) -> None:
        self._prompt = prompt

    def allow_always(self, key: str) -> None:
        self._always.add(key)

    def request(self, *, key: str, title: str, detail: str) -> Decision:
        """Ask whether an action is permitted.

        key    — a stable identifier for "always allow" (e.g. tool name).
        title  — short human label (e.g. "Run shell command").
        detail — the specifics (e.g. the command or file path).
        """
        if self.auto_approve:
            return Decision.ALLOW
        if key in self._always:
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
