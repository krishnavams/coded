"""Check a file right after the agent edits it, and tell the model what broke.

A type error or a syntax slip is cheapest to fix in the same step that made
it. After `write`, `edit`, `multi_edit`, `apply_patch` or `notebook_edit`
changes a file, the command configured for its kind runs on it; if it fails,
its output is appended to the tool result, so the model sees the problem
before it moves on.

Off until configured, because it runs commands:

    "diagnostics": true                       # the built-in checks below
    "diagnostics": {"commands": {"*.py": "mypy {file}", "*.ts": "npx tsc --noEmit"},
                    "builtin": true, "timeout": 20}

`{file}` is the edited file's path (quoted). The built-in checks only use
tools that are installed: Python with ruff (else `python -m py_compile`),
JSON, shell scripts (`bash -n`), and JavaScript (`node --check`).
"""

from __future__ import annotations

import fnmatch
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_TIMEOUT = 20.0
MAX_OUTPUT = 4000


def builtin_commands(which: Callable[[str], Optional[str]] = shutil.which) -> Dict[str, str]:
    py = shlex.quote(sys.executable)
    cmds: Dict[str, str] = {}
    cmds["*.py"] = ("ruff check --quiet --output-format=concise {file}" if which("ruff")
                    else f"{py} -m py_compile {{file}}")
    cmds["*.json"] = (f"{py} -c \"import json, sys; json.load(open(sys.argv[1], "
                      f"encoding='utf-8'))\" {{file}}")
    if which("bash"):
        cmds["*.sh"] = "bash -n {file}"
    if which("node"):
        cmds["*.js"] = "node --check {file}"
        cmds["*.mjs"] = "node --check {file}"
    return cmds


def settings(config: Any, which=shutil.which) -> Tuple[Dict[str, str], float]:
    """(pattern -> command, timeout) from config; empty when off."""
    raw = getattr(config, "diagnostics", None)
    if not raw:
        return {}, DEFAULT_TIMEOUT
    if raw is True:
        raw = {"builtin": True}
    if not isinstance(raw, dict):
        return {}, DEFAULT_TIMEOUT
    cmds: Dict[str, str] = {}
    if raw.get("builtin", not raw.get("commands")):
        cmds.update(builtin_commands(which))
    cmds.update({str(k): str(v) for k, v in (raw.get("commands") or {}).items()})
    return cmds, float(raw.get("timeout", DEFAULT_TIMEOUT))


def command_for(path: str, cmds: Dict[str, str]) -> Optional[str]:
    name = Path(path).name
    hit = None
    for pattern, cmd in cmds.items():  # the last matching pattern wins (user's over built-in)
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(path, pattern):
            hit = cmd
    return hit


def check(paths: List[str], cwd: str, config: Any, run: Optional[Callable] = None) -> str:
    """Run the checks for these files; the failures as text ("" if all pass)."""
    cmds, timeout = settings(config)
    if not cmds:
        return ""
    from coded import shells

    run = run or shells.run
    reports: List[str] = []
    for path in dict.fromkeys(paths):
        cmd = command_for(path, cmds)
        if not cmd or not Path(cwd, path).exists():
            continue
        line = cmd.replace("{file}", shlex.quote(path))
        try:
            res = run(line, cwd=cwd, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - a broken checker must not break the edit
            reports.append(f"$ {line}\n(could not run: {exc})")
            continue
        code = getattr(res, "returncode", 0)
        if code:
            out = ((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")).strip()
            if len(out) > MAX_OUTPUT:
                out = out[:MAX_OUTPUT] + "\n… (cut)"
            reports.append(f"$ {line}\n{out or f'exit code {code}'}")
    if not reports:
        return ""
    return ("\n\n<diagnostics>\nThe file was saved, but its check reports problems. Fix them "
            "before moving on:\n" + "\n\n".join(reports) + "\n</diagnostics>")
