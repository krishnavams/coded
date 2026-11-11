"""OS-level sandbox for shell commands.

Linux uses bubblewrap (``bwrap``); macOS uses ``sandbox-exec`` (Seatbelt). Inside
the sandbox the filesystem is read-only except the working directory, a private
``/tmp``, and any extra ``writable`` paths; the network is cut off unless
``network`` is true.

Config (``"sandbox"`` in config.json)::

    {"enabled": true, "network": false, "writable": ["~/.cache"],
     "auto_allow": true, "fail_if_unavailable": false}

With ``auto_allow`` (the default) sandboxed commands run without a permission
prompt — the sandbox, not the prompt, is the safety boundary. Deny rules, plan
mode and the strict injection guard still apply.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SandboxSettings:
    enabled: bool = False
    network: bool = False
    writable: List[str] = field(default_factory=list)
    auto_allow: bool = True
    fail_if_unavailable: bool = False

    @classmethod
    def from_config(cls, data: Optional[Dict[str, Any]]) -> "SandboxSettings":
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", False)),
            network=bool(data.get("network", False)),
            writable=[str(p) for p in data.get("writable") or []],
            auto_allow=bool(data.get("auto_allow", True)),
            fail_if_unavailable=bool(data.get("fail_if_unavailable", False)),
        )


def available_backend() -> Optional[str]:
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    return None


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def bwrap_argv(command: str, cwd: str, settings: SandboxSettings) -> List[str]:
    cwd = _real(cwd)
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp"]
    for path in [cwd, *(_real(p) for p in settings.writable)]:
        if os.path.exists(path):
            argv += ["--bind", path, path]
    if not settings.network:
        argv.append("--unshare-net")
    argv += ["--unshare-pid", "--die-with-parent", "--chdir", cwd, "bash", "-c", command]
    return argv


def seatbelt_profile(cwd: str, settings: SandboxSettings) -> str:
    paths = [_real(cwd), "/private/tmp", "/private/var/folders",
             *(_real(p) for p in settings.writable)]
    allow = " ".join(f'(subpath "{p}")' for p in paths)
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* {allow} (subpath "/dev"))',
    ]
    if not settings.network:
        lines += ["(deny network*)", "(allow network* (remote unix-socket))"]
    return "\n".join(lines)


def wrap_command(command: str, cwd: str, settings: SandboxSettings,
                 backend: Optional[str] = None) -> Optional[List[str]]:
    """Return an argv running `command` sandboxed, or None if no backend exists."""
    backend = backend or available_backend()
    if backend == "bwrap":
        return bwrap_argv(command, cwd, settings)
    if backend == "sandbox-exec":
        return ["sandbox-exec", "-p", seatbelt_profile(cwd, settings), "bash", "-c", command]
    return None


def sandbox_state(config: Any) -> tuple:
    """(settings, backend) for a Config; backend is None when unavailable/disabled."""
    settings = SandboxSettings.from_config(getattr(config, "sandbox", None))
    if not settings.enabled:
        return settings, None
    return settings, available_backend()
