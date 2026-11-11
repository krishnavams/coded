"""Git worktree isolation for sub-agents and outside agents.

Each isolated task gets its own checkout on a fresh branch, so several agents
can edit in parallel without clobbering each other or the user's working tree.
Worktrees live under ``$XDG_CACHE_HOME/coded/worktrees`` (outside the repo, so
they don't show up in the parent's glob/grep). A worktree with no changes is
removed along with its branch; one with changes is kept for the user to review.
"""

from __future__ import annotations

import os
import subprocess

from coded.shells import text_kwargs
import uuid
from pathlib import Path
from typing import Tuple


class WorktreeError(Exception):
    pass


def _git(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, **text_kwargs())
    if proc.returncode != 0:
        raise WorktreeError((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed")
    return proc.stdout.strip()


def _base_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
    return Path(base) / "coded" / "worktrees"


def create_worktree(cwd: str, label: str = "task") -> Tuple[str, str, str, str]:
    """Create a worktree from HEAD. Returns (worktree_path, branch, repo_root, base_sha).

    The worktree path mirrors `cwd`'s position inside the repo.
    """
    try:
        root = _git(cwd, "rev-parse", "--show-toplevel")
        base = _git(cwd, "rev-parse", "--verify", "HEAD")
    except FileNotFoundError as exc:
        raise WorktreeError("git is not installed.") from exc
    except WorktreeError as exc:
        raise WorktreeError(f"worktree isolation needs a git repo with at least one commit: {exc}")
    slug = "".join(c if c.isalnum() else "-" for c in label.lower())[:24].strip("-") or "task"
    short = uuid.uuid4().hex[:6]
    branch = f"coded/{slug}-{short}"
    path = _base_dir() / f"{Path(root).name}-{slug}-{short}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
    rel = os.path.relpath(os.path.realpath(cwd), os.path.realpath(root))
    sub = path / rel if rel != "." else path
    return str(sub), branch, root, base


def finalize_worktree(path: str, branch: str, root: str, base: str) -> str:
    """Remove the worktree if untouched; otherwise keep it and describe the changes."""
    top = _git(path, "rev-parse", "--show-toplevel")
    status = _git(top, "status", "--porcelain")
    ahead = int(_git(top, "rev-list", "--count", f"{base}..HEAD") or 0)
    if not status and not ahead:
        _git(root, "worktree", "remove", "--force", top)
        _git(root, "branch", "-D", branch)
        return "No file changes were made in the isolated worktree (removed)."
    files = len(status.splitlines())
    return (
        f"Changes kept in worktree {top} on branch '{branch}' "
        f"({files} uncommitted file(s), {ahead} new commit(s)). "
        f"Review with `git -C {top} status` / `git -C {top} diff`; "
        f"bring them over by committing there and merging '{branch}', or copy files. "
        f"Remove with `git worktree remove {top}`."
    )

