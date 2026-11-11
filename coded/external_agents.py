"""Outside coding agents (Claude Code, Codex, Gemini CLI, Aider, ...) as delegates.

coded can hand a self-contained task to another agent CLI and read back its
final answer. Presets are offered automatically when the binary is on PATH
(disable with ``"detect_external_agents": false``). Agents whose binary is not
installed are never offered. Add or override agents in config::

    "external_agents": {
      "claude": {"command": ["claude", "-p", "{prompt}", "--model", "opus"]},
      "my-agent": {"command": ["./bin/agent", "--task-stdin"], "stdin": true,
                   "description": "In-house agent", "timeout": 1800,
                   "env": {"AGENT_MODE": "ci"}},
      "aider": {"enabled": false}
    }

``{prompt}`` and ``{cwd}`` in the command are substituted; with ``"stdin": true``
(or no ``{prompt}`` placeholder) the prompt is written to stdin instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from coded.shells import IS_WINDOWS, run as run_command
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PRESETS: Dict[str, Dict[str, Any]] = {
    "claude": {"command": ["claude", "-p", "{prompt}"],
               "description": "Anthropic Claude Code CLI (non-interactive print mode)"},
    "codex": {"command": ["codex", "exec", "{prompt}"],
              "description": "OpenAI Codex CLI (codex exec)"},
    "gemini": {"command": ["gemini", "-p", "{prompt}"],
               "description": "Google Gemini CLI"},
    "aider": {"command": ["aider", "--yes-always", "--no-auto-commits", "--no-pretty",
                          "--message", "{prompt}"],
              "description": "Aider pair-programming CLI"},
    "opencode": {"command": ["opencode", "run", "{prompt}"],
                 "description": "OpenCode CLI"},
    "cursor": {"command": ["cursor-agent", "-p", "{prompt}"],
               "description": "Cursor Agent CLI"},
    "goose": {"command": ["goose", "run", "-t", "{prompt}"],
              "description": "Block Goose CLI"},
}

_DEFAULT_TIMEOUT = 1800
_MAX_OUTPUT = 40_000


# cmd.exe re-parses the command line of a .bat/.cmd shim, so these characters in
# an argument can end the quoted span and start a new command.
_CMD_META = '"&|<>^%'


def cmd_shim_risk(argv: list) -> Optional[str]:
    """The argument that could be re-parsed by cmd.exe, if any.

    Only applies to .bat/.cmd shims on Windows — anywhere else cmd never sees
    the arguments.
    """
    if not IS_WINDOWS or not argv:
        return None
    if not str(argv[0]).lower().endswith((".bat", ".cmd")):
        return None
    for arg in argv[1:]:
        if any(ch in str(arg) for ch in _CMD_META):
            return str(arg)
    return None


def resolve_argv(argv: list) -> list:
    """argv[0] as a full path. On Windows many CLIs (claude, codex, npm wrappers)
    are .cmd/.bat shims that a bare name in an argv list will not launch."""
    if not argv:
        return argv
    found = shutil.which(argv[0])
    return [found, *argv[1:]] if found else argv


@dataclass
class ExternalAgent:
    name: str
    command: List[str]
    description: str = ""
    timeout: int = _DEFAULT_TIMEOUT
    stdin: bool = False
    env: Dict[str, str] = field(default_factory=dict)

    def argv(self, prompt: str, cwd: str) -> List[str]:
        return [part.replace("{prompt}", prompt).replace("{cwd}", cwd) for part in self.command]

    def uses_stdin(self) -> bool:
        return self.stdin or not any("{prompt}" in p for p in self.command)

    def run(self, prompt: str, cwd: str, max_output: int = _MAX_OUTPUT) -> tuple:
        """Run to completion. Returns (exit_code, combined output)."""
        env = dict(os.environ, **{k: str(v) for k, v in self.env.items()})
        argv = resolve_argv(self.argv(prompt, cwd))
        risky = None if self.uses_stdin() else cmd_shim_risk(argv)
        if risky is not None:
            return 1, (
                f"Not run: '{self.name}' is a Windows .cmd/.bat shim and this prompt contains "
                f"characters cmd.exe would re-parse as commands ({_CMD_META}). Configure the "
                f"agent with \"stdin\": true so the prompt is piped instead of passed as an "
                f"argument, or point `command` at the real executable.")
        proc = run_command(
            argv, cwd=cwd, env=env,
            input=prompt if self.uses_stdin() else None, timeout=self.timeout,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and err:
            out = f"{out}\n\n[stderr]\n{err}".strip()
        if len(out) > max_output:
            out = out[-max_output:] + "\n… (output truncated to the last part)"
        return proc.returncode, out


def resolve_external_agents(config: Any) -> Dict[str, ExternalAgent]:
    configured: Dict[str, Any] = dict(getattr(config, "external_agents", None) or {})
    detect = getattr(config, "detect_external_agents", True)
    agents: Dict[str, ExternalAgent] = {}
    names = list(PRESETS) + [n for n in configured if n not in PRESETS]
    for name in names:
        user = configured.get(name)
        if user is not None and not user.get("enabled", True):
            continue
        spec = {**PRESETS.get(name, {}), **(user or {})}
        command = spec.get("command")
        if not command:
            continue
        if isinstance(command, str):
            command = command.split()
        if user is None and not detect:
            continue
        if shutil.which(command[0]) is None:
            continue  # not installed; never offer the model a tool that can't run
        agents[name] = ExternalAgent(
            name=name,
            command=list(command),
            description=str(spec.get("description", "")),
            timeout=int(spec.get("timeout", _DEFAULT_TIMEOUT)),
            stdin=bool(spec.get("stdin", False)),
            env=dict(spec.get("env") or {}),
        )
    return agents


def describe(agents: Dict[str, ExternalAgent]) -> str:
    return "; ".join(f"{a.name}: {a.description or ' '.join(a.command[:2])}"
                     for a in agents.values())


def which(agent: ExternalAgent) -> Optional[str]:
    return shutil.which(agent.command[0])
