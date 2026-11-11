"""Lifecycle hooks: user-configured shell commands run on agent events.

Config (``"hooks"`` in config.json)::

    {
      "PreToolUse":  [{"matcher": "bash", "command": "./scripts/guard.sh"}],
      "PostToolUse": [{"matcher": "edit|write|multi_edit", "command": "ruff format -q ."}],
      "UserPromptSubmit": [{"command": "cat .coded/context.txt"}],
      "SessionStart": [{"command": "git status --short"}],
      "Stop": [{"command": "pytest -q -x"}]
    }

The Claude Code nested form ``{"matcher": ..., "hooks": [{"type": "command",
"command": ...}]}`` is accepted too. ``matcher`` is a regex matched against the
whole tool name (omit it, or use ``*``, to match everything).

Each hook receives a JSON payload on stdin (``hook_event_name``, ``cwd``,
``tool_name``, ``tool_input``, ``tool_response``, ``prompt``). Protocol:

- exit 0: success. Stdout is added as context for ``UserPromptSubmit`` and
  ``SessionStart``. Stdout JSON ``{"decision": "block", "reason": ...}`` blocks;
  ``{"decision": "allow"}`` on ``PreToolUse`` skips the permission prompt.
- exit 2: block. Stderr is fed back to the model (``PreToolUse`` cancels the
  call, ``PostToolUse`` appends feedback to the result, ``Stop`` makes the agent
  keep working, ``UserPromptSubmit`` rejects the prompt).
- any other exit code: a non-blocking error, reported as a warning.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
_DEFAULT_TIMEOUT = 60


@dataclass
class HookOutcome:
    blocked: bool = False
    allow: bool = False  # PreToolUse explicitly approved the call
    message: str = ""  # reason for a block (fed to the model)
    context: List[str] = field(default_factory=list)  # stdout of successful hooks
    warnings: List[str] = field(default_factory=list)


def _normalize(entries: Any) -> List[Dict[str, Any]]:
    """Flatten both the simple and the Claude Code nested formats."""
    out: List[Dict[str, Any]] = []
    for entry in entries or []:
        if isinstance(entry, str):
            out.append({"command": entry})
        elif isinstance(entry, dict) and "hooks" in entry:
            for h in entry.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    out.append({**h, "matcher": entry.get("matcher")})
        elif isinstance(entry, dict) and entry.get("command"):
            out.append(entry)
    return out


def _matches(matcher: Optional[str], tool_name: Optional[str]) -> bool:
    if not matcher or matcher == "*" or tool_name is None:
        return True
    try:
        return re.fullmatch(matcher, tool_name) is not None
    except re.error:
        return matcher == tool_name


class HookRunner:
    def __init__(self, hooks: Optional[Dict[str, Any]], cwd: str):
        self.cwd = cwd
        self.hooks = {ev: _normalize(hooks.get(ev)) for ev in HOOK_EVENTS} if hooks else {}

    def has(self, event: str) -> bool:
        return bool(self.hooks.get(event))

    def run(self, event: str, payload: Optional[Dict[str, Any]] = None,
            tool_name: Optional[str] = None) -> HookOutcome:
        outcome = HookOutcome()
        for hook in self.hooks.get(event, []):
            if not _matches(hook.get("matcher"), tool_name):
                continue
            self._run_one(hook, event, payload or {}, tool_name, outcome)
            if outcome.blocked:
                break
        return outcome

    def _run_one(self, hook: Dict[str, Any], event: str, payload: Dict[str, Any],
                 tool_name: Optional[str], outcome: HookOutcome) -> None:
        data = {"hook_event_name": event, "cwd": self.cwd, **payload}
        if tool_name is not None:
            data["tool_name"] = tool_name
        env = dict(os.environ, CODED_HOOK_EVENT=event, CODED_PROJECT_DIR=self.cwd)
        timeout = float(hook.get("timeout", _DEFAULT_TIMEOUT))
        try:
            proc = subprocess.run(
                hook["command"], shell=True, cwd=self.cwd, env=env,
                input=json.dumps(data, default=str), capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            outcome.warnings.append(f"{event} hook timed out after {timeout:g}s: {hook['command']}")
            return
        except OSError as exc:
            outcome.warnings.append(f"{event} hook failed to start: {exc}")
            return

        stdout, stderr = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        if proc.returncode == 2:
            outcome.blocked = True
            outcome.message = stderr or stdout or f"Blocked by {event} hook."
            return
        if proc.returncode != 0:
            outcome.warnings.append(
                f"{event} hook exited {proc.returncode}: {hook['command']}"
                + (f"\n{stderr}" if stderr else ""))
            return
        decision = _json_decision(stdout)
        if decision is not None:
            kind = str(decision.get("decision", "")).lower()
            if kind in ("block", "deny"):
                outcome.blocked = True
                outcome.message = str(decision.get("reason") or f"Blocked by {event} hook.")
                return
            if kind in ("allow", "approve"):
                outcome.allow = True
            extra = decision.get("additionalContext") or decision.get("context")
            if extra:
                outcome.context.append(str(extra))
            return
        if stdout:
            outcome.context.append(stdout)


def _json_decision(stdout: str) -> Optional[Dict[str, Any]]:
    if not stdout.startswith("{"):
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
