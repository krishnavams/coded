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
        proc = subprocess.run(
            self.argv(prompt, cwd), cwd=cwd, env=env, capture_output=True, text=True,
            input=prompt if self.uses_stdin() else None,
            stdin=None if self.uses_stdin() else subprocess.DEVNULL,
            timeout=self.timeout,
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
