"""`@path` mentions: point at a file in the message instead of describing it.

`explain @src/app.py` attaches that file's text to the request; `@src/app.py#L10-40`
attaches just those lines, `@docs/` a listing of the folder, and `@shot.png` the
image (for a vision model). A mention must start the message or follow a space,
and must name something that exists under the working directory, so an email
address or a decorator in pasted code is left alone. Quote a path with spaces:
`@"my notes.md"`.

The REPL completes paths after `@` (`MentionCompleter`); `Agent.run` expands
them (`expand_mentions`) for every front-end, `--print` included.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_MENTION = re.compile(r'(?:(?<=\s)|^)@(?:"([^"]+)"|([^\s"]+))')
_LINES = re.compile(r"#L(\d+)(?:-L?(\d+))?$")
_TRAILING = ".,;:!?)]}'"
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
              ".pytest_cache", "dist", "build", ".tox", ".idea", ".coded"}
_MAX_LISTING = 200


@dataclass
class Expanded:
    text: str
    images: List[str] = field(default_factory=list)
    attached: List[str] = field(default_factory=list)   # paths, for a display note


def _resolve(raw: str, cwd: str, dirs: Iterable[str]) -> Optional[Tuple[Path, str, str]]:
    """(path, shown name, line suffix) for a mention, or None if nothing matches.

    Trailing punctuation is dropped if the path only exists without it
    ("look at @app.py, then…")."""
    candidates = [raw]
    stripped = raw.rstrip(_TRAILING)
    if stripped != raw:
        candidates.append(stripped)
    for cand in candidates:
        suffix = ""
        m = _LINES.search(cand)
        if m:
            suffix, cand = m.group(0), cand[: m.start()]
        if not cand:
            continue
        for base in (cwd, *dirs):
            path = Path(base, os.path.expanduser(cand))
            if path.exists():
                return path, cand, suffix
    return None


def _file_block(path: Path, name: str, suffix: str, max_chars: int) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f'<file path="{name}">(could not read: {exc})</file>'
    if b"\0" in data[:8192]:
        return (f'<file path="{name}">(binary file, not attached; use the read tool '
                f'if it is a document)</file>')
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = 1
    m = _LINES.search(suffix) if suffix else None
    if m:
        start = max(int(m.group(1)), 1)
        end = int(m.group(2) or m.group(1))
        lines = lines[start - 1:end]
    body = "\n".join(f"{start + i:>6}\t{line}" for i, line in enumerate(lines))
    note = ""
    if len(body) > max_chars:
        body = body[:max_chars]
        note = "\n(truncated: use the read tool with offset/limit for the rest)"
    return f'<file path="{name}{suffix}">\n{body}{note}\n</file>'


def _dir_block(path: Path, name: str, ignore=None) -> str:
    entries = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name in _SKIP_DIRS or (ignore is not None and ignore(child)):
                continue
            entries.append(child.name + ("/" if child.is_dir() else ""))
            if len(entries) >= _MAX_LISTING:
                entries.append("…")
                break
    except OSError as exc:
        return f'<directory path="{name}">(could not list: {exc})</directory>'
    return f'<directory path="{name}">\n' + "\n".join(entries) + "\n</directory>"


def expand_mentions(text: str, cwd: str, *, max_chars: int = 100_000,
                    vision: bool = True, dirs: Iterable[str] = ()) -> Expanded:
    """Attach what each `@mention` names; the message text itself is kept.
    Paths the project's .codedignore excludes are not attached."""
    from coded.ignore import rules_for

    rules = rules_for(cwd)
    dirs = tuple(dirs)
    blocks: List[str] = []
    out = Expanded(text)
    seen = set()
    budget = max_chars
    for m in _MENTION.finditer(text):
        raw = m.group(1) or m.group(2)
        hit = _resolve(raw, cwd, dirs)
        if hit is None:
            continue
        path, name, suffix = hit
        if rules and rules.ignored(path):
            blocks.append(f'<file path="{name}">(excluded by .codedignore; not attached)</file>')
            out.attached.append(name + " (excluded)")
            continue
        key = (str(path), suffix)
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir():
            blocks.append(_dir_block(path, name, rules.ignored if rules else None))
        elif path.suffix.lower() in IMAGE_EXTS:
            if vision:
                out.images.append(str(path))
            else:
                blocks.append(f'<file path="{name}">(an image; this model cannot see '
                              f'images)</file>')
        else:
            block = _file_block(path, name, suffix, max(budget, 2000))
            budget -= len(block)
            blocks.append(block)
        out.attached.append(name + suffix)
    if blocks:
        out.text = text + "\n\n<mentioned-files>\n" + "\n\n".join(blocks) + "\n</mentioned-files>"
    return out


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------
def list_paths(cwd: str, limit: int = 5000, ignore=None) -> List[str]:
    """Files and folders under cwd (relative, `/`-separated), skipping VCS and
    dependency folders, for completion. Bounded so a huge tree stays fast."""
    out: List[str] = []
    root = Path(cwd)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS
                             and not d.startswith("."))
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        for d in dirnames:
            out.append(prefix + d + "/")
        for f in sorted(filenames):
            out.append(prefix + f)
        if ignore is not None:
            out = [p for p in out if not ignore(p)]
        if len(out) >= limit:
            break
    return out[:limit]


def rank_paths(query: str, paths: Iterable[str], limit: int = 30) -> List[str]:
    """Paths matching `query`: prefix of the name first, then substring, then
    the letters in order (fuzzy)."""
    q = query.lower()
    scored = []
    for p in paths:
        low = p.lower()
        base = low.rstrip("/").rsplit("/", 1)[-1]
        if not q:
            score = 3
        elif low.startswith(q) or base.startswith(q):
            score = 0
        elif q in low:
            score = 1
        else:
            it = iter(low)
            if not all(ch in it for ch in q):
                continue
            score = 2
        scored.append((score, len(p), p))
    scored.sort()
    return [p for _, _, p in scored[:limit]]


try:  # prompt_toolkit is a core dependency, but keep this module importable alone
    from prompt_toolkit.completion import Completer, Completion
except ImportError:  # pragma: no cover
    Completer = object  # type: ignore[assignment,misc]
    Completion = None  # type: ignore[assignment]


class MentionCompleter(Completer):  # type: ignore[misc]
    """Completes the word after `@` with paths under cwd."""

    def __init__(self, cwd: str, ignore=None):
        self.cwd = cwd
        self.ignore = ignore
        self._paths: Optional[List[str]] = None

    def refresh(self) -> None:
        self._paths = None

    def get_completions(self, document, complete_event):
        word = document.get_word_before_cursor(WORD=True)
        if not word.startswith("@"):
            return
        if self._paths is None:
            ignore = self.ignore
            if ignore is None:
                from coded.ignore import rules_for

                rules = rules_for(self.cwd)
                if rules:
                    ignore = lambda p: rules.ignored_rel(p.rstrip("/"), p.endswith("/"))  # noqa: E731
            self._paths = list_paths(self.cwd, ignore=ignore)
        query = word[1:]
        for path in rank_paths(query, self._paths):
            shown = f'@"{path}"' if " " in path else "@" + path
            yield Completion(shown, start_position=-len(word), display=path,
                             display_meta="folder" if path.endswith("/") else "file")
