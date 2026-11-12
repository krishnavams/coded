"""`coded doctor`: is everything coded needs here, and does it work?

Checks the Python version, the required and optional packages, the config
files and every model's key, the shell, the sandbox, git, the terminal and the
helpers some features use (clipboard, notifications, the browser). With
`--network` it also probes each model's endpoint (the same staged probe as
`coded models --check`). Exit code 1 when something is broken.
"""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    area: str
    status: str
    detail: str


def _version(module: str) -> Optional[str]:
    try:
        from importlib.metadata import version

        return version(module)
    except Exception:  # noqa: BLE001
        try:
            return getattr(importlib.import_module(module), "__version__", "installed")
        except Exception:  # noqa: BLE001
            return None


def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def run_checks(cwd: str, *, config_path: Optional[str] = None, network: bool = False,
               which: Callable[[str], Optional[str]] = shutil.which,
               isatty: Callable[[], bool] = lambda: sys.stdout.isatty()) -> List[Check]:
    from coded import __version__

    out: List[Check] = []
    py = sys.version_info
    out.append(Check("Python", OK if py >= (3, 10) else FAIL,
                     f"{py.major}.{py.minor}.{py.micro} ({sys.executable})"
                     + ("" if py >= (3, 10) else " — coded needs 3.10 or newer")))
    out.append(Check("coded", OK, f"{__version__} ({os.path.dirname(os.path.abspath(__file__))})"))

    for pkg, mod in (("openai", "openai"), ("httpx", "httpx"), ("rich", "rich"),
                     ("prompt_toolkit", "prompt_toolkit")):
        ok = _importable(mod)
        out.append(Check(pkg, OK if ok else FAIL,
                         (_version(pkg) or "installed") if ok else "missing — pip install -e ."))
    for extra, mods, use in (("tui", ["textual"], "coded --tui"),
                             ("mcp", ["mcp"], "MCP servers"),
                             ("docs", ["pypdf", "docx", "openpyxl", "pptx"], "PDF/Word/Excel/PowerPoint"),
                             ("browser", ["playwright"], "the browser tool")):
        missing = [m for m in mods if not _importable(m)]
        hint = f"pip install 'coded[{extra}]'" + (
            " && playwright install chromium" if extra == "browser" else "")
        out.append(Check(f"extra: {extra}", OK if not missing else WARN,
                         f"{use}: available" if not missing
                         else f"{use}: not installed ({hint})"))

    # -- config and models -----------------------------------------------------
    cfg = None
    try:
        from coded.config import ConfigError, load_config

        cfg = load_config(cwd, config_path)
        files = ", ".join(str(p) for p in cfg.source_paths) or "none (defaults and environment)"
        out.append(Check("config", OK, f"loaded: {files}"))
    except Exception as exc:  # noqa: BLE001 - ConfigError, bad JSON, …
        out.append(Check("config", FAIL, str(exc).splitlines()[0]))
    if cfg is not None:
        if not cfg.models:
            out.append(Check("models", FAIL, "none configured — run `coded config setup`"))
        else:
            default = cfg.default_model
            out.append(Check("models", OK if default in cfg.models else WARN,
                             f"{len(cfg.models)} configured; default: {default or 'none'}"))
            for name, mc in cfg.models.items():
                try:
                    mc.resolved_api_key()
                    out.append(Check(f"model {name}", OK,
                                     f"{mc.model} at {mc.base_url or mc.provider}"))
                except ConfigError as exc:
                    out.append(Check(f"model {name}", FAIL, str(exc)))
        for w in cfg.model_warnings() + list(cfg.plugin_warnings):
            out.append(Check("config warning", WARN, w))

        if network and cfg.models:
            from coded.llm import probe_stages

            for name, mc in cfg.models.items():
                try:
                    stages = probe_stages(mc)
                except Exception as exc:  # noqa: BLE001
                    out.append(Check(f"endpoint {name}", FAIL, f"{type(exc).__name__}: {exc}"))
                    continue
                bad = [label for label, exc in stages if exc is not None]
                out.append(Check(f"endpoint {name}", FAIL if bad else OK,
                                 f"refuses: {', '.join(bad)} (coded models --check {name})" if bad
                                 else f"{len(stages)} request shapes accepted"))

    # -- environment -------------------------------------------------------------
    try:
        from coded.shells import shell_summary

        label, _posix = shell_summary()
        out.append(Check("shell", OK, label))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("shell", FAIL, str(exc)))
    from coded.sandbox import available_backend

    backend = available_backend()
    out.append(Check("sandbox", OK if backend else WARN,
                     backend or "no backend (Linux: install bubblewrap; macOS has sandbox-exec)"))
    out.append(Check("git", OK if which("git") else WARN,
                     which("git") or "not found — the git tool and worktrees need it"))
    if isatty():
        size = shutil.get_terminal_size()
        out.append(Check("terminal", OK if size.columns >= 60 else WARN,
                         f"{size.columns}×{size.lines}"
                         + ("" if size.columns >= 60 else " — narrow; the status line is cut")))
    else:
        out.append(Check("terminal", WARN, "not a terminal (fine for --print and CI)"))

    if cfg is not None:
        from coded.lsp import find_command, server_specs

        if not cfg.lsp:
            out.append(Check("code intelligence", OK,
                             'off (set "lsp": true to use language servers)'))
        else:
            specs, _ = server_specs(cfg.lsp)
            found = [(s.language, find_command(s, which)) for s in specs]
            have = [f"{lang}: {os.path.basename(argv[0])}" for lang, argv in found if argv]
            missing = [lang for lang, argv in found if not argv]
            out.append(Check("code intelligence", OK if have else WARN,
                             (", ".join(have) if have else "no language server found on PATH")
                             + (f" · not found: {', '.join(missing)}" if missing else "")))

    from coded.images import clipboard_commands

    clip = clipboard_commands("x.png", sys.platform, which)
    out.append(Check("clipboard images", OK if clip else WARN,
                     clip[0][0][0] if clip else "no reader (Linux: wl-clipboard or xclip)"))
    if cfg is not None and getattr(cfg, "notify", "bell") == "desktop":
        from coded.notify import desktop_command

        cmd = desktop_command("t", "m", sys.platform, which)
        out.append(Check("notifications", OK if cmd else WARN,
                         cmd[0] if cmd else "no desktop notifier; the bell is used"))

    from coded.update_check import cached, checkout_root

    root = checkout_root()
    if root is not None:
        info = cached()
        if info.get("behind"):
            out.append(Check("updates", WARN, f"a newer version is available: git -C {root} pull"))
        else:
            out.append(Check("updates", OK, "up to date" if info else "not checked yet"))
    return out


def render(checks: List[Check]) -> str:
    width = max(len(c.area) for c in checks)
    lines = [f"{MARK[c.status]} {c.area:<{width}}  {c.detail}" for c in checks]
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append(f"{fails} problem(s), {warns} warning(s)." if fails or warns
                 else "Everything looks good.")
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="coded doctor",
                                description="Check that coded's requirements and config work here.")
    p.add_argument("--network", action="store_true",
                   help="Also probe every model's endpoint (sends small requests).")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Project directory (default: current).")
    args = p.parse_args(argv)
    checks = run_checks(args.cwd or os.getcwd(), config_path=args.config, network=args.network)
    print(render(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0
