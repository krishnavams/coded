"""The project's digital twin: a kept-up-to-date model of the codebase.

Per file, what coded has already worked out — its imports (before resolution)
and its code-health findings — keyed by (size, mtime, parser version), so a
file that has not changed is never read or parsed again. The dependency map
(`depgraph`) and the refactor scan (`codehealth`) use it when it is passed,
and give the same answers either way (tested).

Kept in the user's cache directory, keyed by the project's real path — never
inside the repository, where a cloned repo could ship a crafted cache. Written
atomically; a corrupt or foreign-version file is simply rebuilt. Refreshed
lazily: when `/twin` (or a cached scan) runs, never by a watcher thread.

`/twin` shows the snapshot: languages, modules, the files most of the code
depends on, import cycles, refactor hot spots, and what changed since the
last time it looked.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

PARSER_VERSION = 1


def _version() -> str:
    """Cached facts depend on the parsers and on thresholds that can change in
    any release, so a new coded version starts a fresh twin."""
    from coded import __version__

    return f"{PARSER_VERSION}:{__version__}"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "coded" / "twin"


class Twin:
    def __init__(self, cwd: str, path: Optional[Path] = None):
        self.root = os.path.realpath(cwd)
        key = hashlib.sha256(self.root.encode("utf-8")).hexdigest()[:24]
        self.path = path or cache_dir() / f"{key}.json"
        self.files: Dict[str, Dict[str, Any]] = {}
        self.snapshot: Dict[str, List[float]] = {}   # rel -> [size, mtime] at the last /twin
        self.taken: Optional[float] = None
        self.hits = self.misses = 0
        self._load()

    # -- storage ----------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != _version() \
                or data.get("root") != self.root:
            return
        files = data.get("files")
        self.files = files if isinstance(files, dict) else {}
        snap = data.get("snapshot")
        self.snapshot = snap if isinstance(snap, dict) else {}
        self.taken = data.get("taken")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"version": _version(), "root": self.root,
                                       "files": self.files, "snapshot": self.snapshot,
                                       "taken": self.taken}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # a cache that cannot be written only costs speed

    # -- entries ----------------------------------------------------------------
    def entry(self, rel: str) -> Optional[Dict[str, Any]]:
        """The cached facts for a file (a dict to read from and fill in), fresh
        for its current size and mtime; None if the file cannot be stat'ed."""
        try:
            st = os.stat(os.path.join(self.root, rel))
        except OSError:
            self.files.pop(rel, None)
            return None
        stamp = [st.st_size, st.st_mtime_ns]
        cur = self.files.get(rel)
        if isinstance(cur, dict) and cur.get("stamp") == stamp:
            self.hits += 1
            return cur
        self.misses += 1
        cur = {"stamp": stamp}
        self.files[rel] = cur
        return cur

    def prune(self, keep: List[str]) -> None:
        wanted = set(keep)
        for rel in [r for r in self.files if r not in wanted]:
            del self.files[rel]


# ---------------------------------------------------------------------------
# /twin
# ---------------------------------------------------------------------------
_LANG = {".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
         ".mjs": "JavaScript", ".cjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
         ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby",
         ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cs": "C#",
         ".php": "PHP", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell"}


def refresh(cwd: str, twin: Optional[Twin] = None) -> Dict[str, Any]:
    """Bring the twin up to date and return the snapshot facts for `/twin`."""
    from coded import codehealth, depgraph
    from coded.repo_overview import list_files

    twin = twin or Twin(cwd)
    start = time.monotonic()
    try:
        files, _ = list_files(cwd)
    except Exception:  # noqa: BLE001
        files = []
    try:
        from coded.ignore import rules_for

        rules = rules_for(cwd)
        ignored = rules.ignored if rules else None
    except Exception:  # noqa: BLE001
        ignored = None
    if ignored is not None:
        files = [f for f in files if not ignored(Path(cwd) / f)]
    graph = depgraph.build(cwd, files, twin=twin)
    findings, truncated = codehealth.scan(cwd, files, twin=twin)

    # What changed since the last /twin.
    now: Dict[str, List[float]] = {}
    for rel in files:
        try:
            st = os.stat(os.path.join(cwd, rel))
            now[rel] = [st.st_size, st.st_mtime_ns]
        except OSError:
            continue
    before = twin.snapshot
    added = sorted(r for r in now if r not in before)
    removed = sorted(r for r in before if r not in now)
    changed = sorted(r for r in now if r in before and list(before[r]) != now[r])
    previous = twin.taken
    twin.snapshot, twin.taken = now, time.time()
    twin.prune(files)
    twin.save()

    langs = Counter(_LANG.get(Path(f).suffix.lower()) for f in files)
    langs.pop(None, None)
    modules: Dict[str, int] = defaultdict(int)
    for f in files:
        parts = Path(f).parts
        modules[parts[0] if len(parts) > 1 else "(root)"] += 1
    rev = graph.dependents
    central = sorted(((len(v), k) for k, v in rev.items()), reverse=True)[:8]
    return {
        "files": len(files), "languages": dict(langs.most_common()),
        "modules": dict(sorted(modules.items(), key=lambda kv: -kv[1])[:10]),
        "central": [(k, n) for n, k in central],
        "cycles": depgraph.cycles(graph),
        "hot_spots": findings[:6], "hot_spot_count": len(findings),
        "added": added, "removed": removed, "changed": changed,
        "previous": previous, "first": previous is None,
        "cache": (twin.hits, twin.misses), "seconds": time.monotonic() - start,
        "truncated": truncated or graph.truncated,
    }


def render(facts: Dict[str, Any]):
    from rich.box import ROUNDED
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    from coded import ui
    from coded.tasks import format_elapsed

    t = Text()
    langs = ", ".join(f"{k} {v}" for k, v in facts["languages"].items()) or "no code files"
    t.append(f"{facts['files']} files", style="bold")
    t.append(f"  ·  {langs}\n", style=ui.MUTED)
    t.append("modules  ", style=ui.MUTED)
    t.append(", ".join(f"{k} ({v})" for k, v in facts["modules"].items()) + "\n")
    if facts["central"]:
        t.append("\nWhat the rest depends on most:\n", style=f"bold {ui.ACCENT}")
        for path, n in facts["central"]:
            t.append(f"  {path}", style="white")
            t.append(f"  imported by {n}\n", style=ui.MUTED)
    cycles = facts["cycles"]
    t.append("\nImport cycles: ", style=f"bold {ui.ACCENT}")
    t.append(f"{len(cycles)}\n" if cycles else "none\n", style=ui.WARN if cycles else ui.ACCENT2)
    for group in cycles[:3]:
        t.append("  " + " ↔ ".join(group) + "\n", style=ui.MUTED)
    t.append("\nRefactor hot spots: ", style=f"bold {ui.ACCENT}")
    t.append(f"{facts['hot_spot_count']}\n")
    for f in facts["hot_spots"]:
        t.append(f"  {f.path}:{f.line} {f.kind} {f.name}\n", style=ui.MUTED)
    t.append("\nSince the last look", style=f"bold {ui.ACCENT}")
    if facts["first"]:
        t.append(": first snapshot of this project.\n", style=ui.MUTED)
    else:
        t.append(f" ({format_elapsed(time.time() - facts['previous'])} ago): ", style=ui.MUTED)
        moved = [("added", facts["added"], ui.ACCENT2), ("changed", facts["changed"], ui.WARN),
                 ("removed", facts["removed"], ui.ERR)]
        if not any(items for _, items, _ in moved):
            t.append("nothing changed.\n", style=ui.MUTED)
        else:
            t.append("\n")
            for label, items, colour in moved:
                if items:
                    t.append(f"  {label} {len(items)}: ", style=colour)
                    t.append(", ".join(items[:8]) + (" …" if len(items) > 8 else "") + "\n")
    _, misses = facts["cache"]
    read = (f"re-read {misses} new or changed file(s)" if misses else
            "every file came from the twin")
    foot = Text(f"{facts['seconds']:.1f}s · {read}"
                + (" · scan capped" if facts["truncated"] else ""), style=ui.MUTED)
    return Group(Panel(t, title=f"[bold {ui.ACCENT}]Project twin[/]", title_align="left",
                       border_style=ui.ACCENT, box=ROUNDED), foot)
