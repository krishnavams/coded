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

# What `coded agents` tells the user about each preset. Install commands are
# shown, never run: installing a program is the user's decision.
_NPM = "npm install -g {}"
AGENT_INFO: Dict[str, Dict[str, Any]] = {
    "aider": {"open_source": True, "homepage": "https://aider.chat",
              "install": "python -m pip install aider-install && aider-install", "needs": "python"},
    "opencode": {"open_source": True, "homepage": "https://opencode.ai",
                 "install": _NPM.format("opencode-ai"), "needs": "npm"},
    "goose": {"open_source": True, "homepage": "https://github.com/block/goose",
              "install": "curl -fsSL https://github.com/block/goose/releases/download/stable/"
                         "download_cli.sh | bash",
              "install_mac": "brew install block-goose-cli",
              "install_windows": "see https://block.github.io/goose/docs/getting-started/installation"},
    "codex": {"open_source": True, "homepage": "https://github.com/openai/codex",
              "install": _NPM.format("@openai/codex"), "needs": "npm"},
    "gemini": {"open_source": True, "homepage": "https://github.com/google-gemini/gemini-cli",
               "install": _NPM.format("@google/gemini-cli"), "needs": "npm"},
    "claude": {"open_source": False, "homepage": "https://docs.anthropic.com/en/docs/claude-code",
               "install": _NPM.format("@anthropic-ai/claude-code"), "needs": "npm"},
    "cursor": {"open_source": False, "homepage": "https://cursor.com/cli",
               "install": "curl https://cursor.com/install -fsS | bash",
               "install_windows": "see https://cursor.com/cli"},
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


# ---------------------------------------------------------------------------
# `coded agents`: which outside agents are usable, and how to get the others
# ---------------------------------------------------------------------------
@dataclass
class AgentStatus:
    name: str
    state: str              # ready | not installed | disabled | detection off
    command: List[str]
    description: str = ""
    path: Optional[str] = None
    configured: bool = False     # defined or changed in the user's config
    open_source: Optional[bool] = None
    homepage: str = ""
    install: str = ""            # a command for the user to run, or a pointer
    note: str = ""               # e.g. "needs Node.js (npm not found)"


def install_hint(name: str, platform: str = "", which=None) -> tuple:
    """(command to show, note) for installing a preset agent on this platform."""
    import sys

    which = which or shutil.which
    platform = platform or sys.platform
    info = AGENT_INFO.get(name) or {}
    if platform.startswith("win") and info.get("install_windows"):
        cmd = info["install_windows"]
    elif platform == "darwin" and info.get("install_mac"):
        cmd = info["install_mac"]
    else:
        cmd = info.get("install", "")
    note = ""
    needs = info.get("needs")
    if needs == "npm" and cmd.startswith("npm ") and which("npm") is None:
        note = "needs Node.js 18+ (npm not found)"
    elif needs == "python" and which("python") is None and which("python3") is None:
        note = "needs Python 3 (python not found)"
    if needs == "python" and which("python") is None and which("python3") is not None:
        cmd = cmd.replace("python -m", "python3 -m")
    return cmd, note


def agent_statuses(config: Any, which=None, platform: str = "") -> List[AgentStatus]:
    """Every preset and configured agent, whether the model can use it, and why not."""
    which = which or shutil.which
    configured: Dict[str, Any] = dict(getattr(config, "external_agents", None) or {})
    detect = getattr(config, "detect_external_agents", True)
    out: List[AgentStatus] = []
    for name in list(PRESETS) + [n for n in configured if n not in PRESETS]:
        user = configured.get(name)
        spec = {**PRESETS.get(name, {}), **(user or {})}
        command = spec.get("command") or []
        if isinstance(command, str):
            command = command.split()
        info = AGENT_INFO.get(name, {})
        status = AgentStatus(name=name, state="ready", command=list(command),
                             description=str(spec.get("description", "")),
                             configured=user is not None,
                             open_source=info.get("open_source"),
                             homepage=info.get("homepage", ""))
        status.path = which(command[0]) if command else None
        if user is not None and not user.get("enabled", True):
            status.state = "disabled"
        elif not command:
            status.state = "not installed"
            status.note = "no `command` in config"
        elif status.path is None:
            status.state = "not installed"
            if name in AGENT_INFO and not (user and user.get("command")):
                status.install, status.note = install_hint(name, platform, which)
            else:
                status.note = f"`{command[0]}` is not on PATH"
        elif user is None and not detect:
            status.state = "detection off"
        out.append(status)
    return out


def version_of(path: str, timeout: float = 15.0) -> tuple:
    """(ok, first line of `<agent> --version`): proves the program starts."""
    try:
        proc = run_command([path, "--version"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"no answer to --version within {timeout:.0f}s"
    except OSError as exc:
        return False, f"could not start: {exc}"
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}: {first[:120]}" if first else f"exit {proc.returncode}"
    return True, first[:120] or "(started, printed no version)"
