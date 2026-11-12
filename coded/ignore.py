"""`.codedignore`: paths the agent must never read or change.

Same syntax as `.gitignore`: one pattern per line, `#` comments, `!` to
re-include, a trailing `/` for folders only, a leading `/` (or any other `/`)
to anchor the pattern to the project root, and `**` for any number of folders.
A folder that matches hides everything inside it.

It is enforced where coded itself touches files: the file tools (read, write,
edit, multi_edit, notebook_edit, move, delete, ls), grep/glob results, `@`
mentions and their completion. A shell command run through `bash` can still
reach these files — say so in the project instructions, or use a deny rule
such as `bash(*secrets*)`, when that matters.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

FILENAME = ".codedignore"


@dataclass
class _Rule:
    regex: "re.Pattern[str]"
    negate: bool
    dir_only: bool


def _translate(pattern: str) -> Tuple[str, bool]:
    """A gitignore pattern as a regex over `/`-separated relative paths, and
    whether it is anchored to the root."""
    anchored = "/" in pattern.rstrip("/")
    pattern = pattern.lstrip("/")
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out += "/.*"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        else:
            ch = pattern[i]
            if ch == "*":
                out += "[^/]*"
            elif ch == "?":
                out += "[^/]"
            elif ch == "[":
                j = pattern.find("]", i)
                if j == -1:
                    out += re.escape(ch)
                else:
                    out += fnmatch.translate(pattern[i:j + 1])[4:-3]
                    i = j
            else:
                out += re.escape(ch)
            i += 1
    return out, anchored


class IgnoreRules:
    def __init__(self, root: str, lines: List[str]):
        self.root = Path(root).resolve()
        self.rules: List[_Rule] = []
        for raw in lines:
            line = raw.rstrip("\n").rstrip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            dir_only = line.endswith("/")
            body, anchored = _translate(line.rstrip("/"))
            prefix = "" if anchored else "(?:.*/)?"
            self.rules.append(_Rule(re.compile(f"^{prefix}{body}$"), negate, dir_only))

    def __bool__(self) -> bool:
        return bool(self.rules)

    @classmethod
    def load(cls, root: str) -> "IgnoreRules":
        path = Path(root) / FILENAME
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        return cls(root, lines)

    def _match(self, rel: str, is_dir: bool) -> Optional[bool]:
        result = None
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(rel):
                result = not rule.negate
        return result

    def ignored_rel(self, rel: str, is_dir: bool = False) -> bool:
        """For a `/`-separated path relative to the root."""
        rel = rel.strip("/")
        if not rel or not self.rules:
            return False
        parts = rel.split("/")
        # A matching parent folder hides everything below it.
        for n in range(1, len(parts)):
            if self._match("/".join(parts[:n]), True):
                return True
        return bool(self._match(rel, is_dir))

    def ignored(self, path: os.PathLike | str) -> bool:
        """For any path; False outside the root."""
        if not self.rules:
            return False
        p = Path(path)
        try:
            rel = p.resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return False
        return self.ignored_rel(rel, p.is_dir())


_cache: dict = {}


def rules_for(root: str) -> IgnoreRules:
    """The project's rules, re-read when `.codedignore` changes."""
    path = Path(root) / FILENAME
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    key = str(Path(root).resolve())
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    rules = IgnoreRules.load(root) if stamp is not None else IgnoreRules(root, [])
    _cache[key] = (stamp, rules)
    return rules


def refusal(path: str) -> str:
    return (f"'{path}' is excluded by the project's {FILENAME}: the user does not want the "
            f"agent to read or change it. Do not try to reach it another way (such as "
            f"through bash); ask the user if you need something from it.")
