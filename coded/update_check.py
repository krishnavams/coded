"""Tell the user when their copy of coded is behind.

coded is usually installed from a git clone (`pip install -e .`), so the
reliable "latest version" is the clone's own remote: once a day, in the
background, `git ls-remote` asks for the remote's HEAD and compares it with the
local one. The answer is cached in ~/.cache/coded/update.json and shown at the
*next* start, so checking never slows start-up. Installs that are not git
checkouts are not checked (there is no source to compare with that could not
be confused with an unrelated package of the same name).

Off with `"update_check": false` (or CODED_NO_UPDATE_CHECK=1).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

DAY = 24 * 3600


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
    return Path(base) / "coded" / "update.json"


def checkout_root() -> Optional[Path]:
    """The git checkout coded runs from, if it is one."""
    here = Path(__file__).resolve().parent.parent
    return here if (here / ".git").exists() else None


def _git(args, cwd: Path, run=None) -> Optional[str]:
    import subprocess

    run = run or subprocess.run
    try:
        res = run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 - no git, no network: just skip
        return None
    if getattr(res, "returncode", 1) != 0:
        return None
    return (res.stdout or "").strip()


def check_now(root: Path, run=None) -> dict:
    """Compare the local HEAD with the remote's (network)."""
    local = _git(["rev-parse", "HEAD"], root, run)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root, run) or "HEAD"
    ref = branch if branch != "HEAD" else "HEAD"
    remote_out = _git(["ls-remote", "origin", ref], root, run)
    remote = remote_out.split()[0] if remote_out else None
    behind = None
    if local and remote and local != remote:
        known = _git(["cat-file", "-e", f"{remote}^{{commit}}"], root, run)
        # A remote commit we already have is older than ours (or on our
        # history): not behind. One we don't have means there is something new.
        behind = known is None
    elif local and remote:
        behind = False
    return {"checked": time.time(), "root": str(root), "branch": branch,
            "local": local, "remote": remote, "behind": behind}


def cached() -> dict:
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def notice(enabled: bool = True, *, root: Optional[Path] = None, now: Callable[[], float] = time.time,
           start_check: Callable[[Path], None] = None) -> Optional[str]:
    """The message to show at start-up (from the last check), and start a new
    background check if the last one is more than a day old."""
    if not enabled or os.environ.get("CODED_NO_UPDATE_CHECK"):
        return None
    root = root if root is not None else checkout_root()
    if root is None:
        return None
    info = cached()
    if now() - float(info.get("checked", 0)) > DAY:
        (start_check or _start_background_check)(root)
    if info.get("behind") and info.get("root") == str(root):
        return (f"A newer coded is available on '{info.get('branch', 'main')}'. "
                f"Update with: git -C {root} pull")
    return None


def _start_background_check(root: Path) -> None:
    def work() -> None:
        info = check_now(root)
        try:
            path = cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(info), encoding="utf-8")
        except OSError:
            pass

    threading.Thread(target=work, daemon=True).start()
