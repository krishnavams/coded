"""Code health: where refactoring would pay off.

A plain scan, no model call: Python functions measured with `ast` (length,
branches, nesting depth, parameters) and copy-pasted blocks found in any code
file by hashing normalized line windows. Findings are ranked by how far past
the thresholds they are, weighted by how many files import the file (a messy
function everyone depends on matters more). Bounded and never raises; it backs
`repo_map` `operation: health` and the built-in `refactor` skill.
"""

from __future__ import annotations

import ast
import hashlib
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

MAX_FILES = 1_500
MAX_SECONDS = 6.0
LONG_FUNCTION = 60      # lines
MANY_BRANCHES = 12      # decision points
DEEP_NESTING = 4        # nested blocks
MANY_PARAMS = 6
DUP_WINDOW = 6          # identical non-trivial lines in a row
_MIN_DUP_LINE = 12      # shorter lines (`}`, `return x`) prove nothing
_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.ExceptHandler,
             ast.With, ast.AsyncWith, ast.IfExp, ast.BoolOp, ast.comprehension, ast.Assert)
_NESTERS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)
_COMMENT = re.compile(r"^\s*(#|//|/\*|\*|--)")


@dataclass
class Finding:
    path: str
    line: int
    kind: str          # long-function, complex, deep-nesting, many-params, duplicate
    name: str
    detail: str
    score: float

    def render(self) -> str:
        return f"{self.path}:{self.line}  {self.kind}  {self.name} — {self.detail}"


def _depth(node: ast.AST, level: int = 0) -> int:
    deepest = level
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.If) and isinstance(child, ast.If) and node.orelse == [child]:
            nxt = level  # an elif continues the chain; it is not a deeper block
        else:
            nxt = level + 1 if isinstance(child, _NESTERS) else level
        deepest = max(deepest, _depth(child, nxt))
    return deepest


def _functions(tree: ast.AST):
    stack: List[Tuple[ast.AST, str]] = [(tree, "")]
    while stack:
        node, prefix = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                yield name, child
                stack.append((child, name + "."))
            elif isinstance(child, ast.ClassDef):
                stack.append((child, f"{prefix}{child.name}."))
            else:
                stack.append((child, prefix))


def python_findings(rel: str, text: str) -> List[Finding]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out: List[Finding] = []
    for name, fn in _functions(tree):
        end = getattr(fn, "end_lineno", fn.lineno) or fn.lineno
        length = end - fn.lineno + 1
        own = [n for n in ast.walk(fn) if n is not fn]
        branches = sum(isinstance(n, _BRANCHES) for n in own)
        depth = _depth(fn)
        a = fn.args
        params = len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
        if a.args and a.args[0].arg in ("self", "cls"):
            params -= 1
        if length > LONG_FUNCTION:
            out.append(Finding(rel, fn.lineno, "long-function", name,
                               f"{length} lines (over {LONG_FUNCTION}): split into steps",
                               length / LONG_FUNCTION))
        if branches > MANY_BRANCHES:
            out.append(Finding(rel, fn.lineno, "complex", name,
                               f"{branches} decision points (over {MANY_BRANCHES}): extract "
                               "cases or use a table", branches / MANY_BRANCHES))
        if depth > DEEP_NESTING:
            out.append(Finding(rel, fn.lineno, "deep-nesting", name,
                               f"{depth} levels deep (over {DEEP_NESTING}): return early",
                               depth / DEEP_NESTING))
        if params > MANY_PARAMS:
            out.append(Finding(rel, fn.lineno, "many-params", name,
                               f"{params} parameters (over {MANY_PARAMS}): group them",
                               params / MANY_PARAMS))
    return out


def _windows(text: str) -> List[Tuple[int, str]]:
    """(first line number, hash) for every run of DUP_WINDOW meaningful lines."""
    lines = [(i + 1, " ".join(ln.split())) for i, ln in enumerate(text.splitlines())]
    keep = [(n, ln) for n, ln in lines if len(ln) >= _MIN_DUP_LINE and not _COMMENT.match(ln)
            and not ln.startswith(("import ", "from ", "#include", "using "))]
    out = []
    for i in range(len(keep) - DUP_WINDOW + 1):
        chunk = "\n".join(ln for _, ln in keep[i:i + DUP_WINDOW])
        # 12 hex digits: plenty to tell blocks apart, and keeps the twin small.
        out.append((keep[i][0], hashlib.sha1(chunk.encode("utf-8")).hexdigest()[:12]))
    return out


def duplicate_findings(texts: Dict[str, str], limit: int = 40) -> List[Finding]:
    """Blocks of DUP_WINDOW+ identical lines in two or more places."""
    return _duplicates_from({rel: _windows(text) for rel, text in texts.items()}, limit)


def _duplicates_from(windows: Dict[str, List[Tuple[int, str]]], limit: int = 40) -> List[Finding]:
    seen: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for rel, items in windows.items():
        for line, digest in items:
            seen[digest].append((rel, line))
    # One finding per pair of places; consecutive windows of the same copy merge.
    reported: Dict[Tuple[str, str], Tuple[int, int, int]] = {}
    for places in seen.values():
        uniq = sorted(set(places))
        if len(uniq) < 2:
            continue
        first = uniq[0]
        for other in uniq[1:]:
            key = (f"{first[0]}:{first[1] // 50}", other[0])
            prev = reported.get(key)
            if prev is None:
                reported[key] = (first[1], other[1], DUP_WINDOW)
            else:
                reported[key] = (prev[0], prev[1], prev[2] + 1)
    out = []
    for (where, other), (line, other_line, size) in reported.items():
        path = where.rsplit(":", 1)[0]
        same = " (same file)" if path == other else ""
        out.append(Finding(path, line, "duplicate", f"{size}+ lines",
                           f"also at {other}:{other_line}{same}: extract a shared function",
                           size / DUP_WINDOW))
    return sorted(out, key=lambda f: -f.score)[:limit]


def scan(cwd: str, files: Sequence[str], *, ignored=None, max_files: int = MAX_FILES,
         deadline: Optional[float] = None, twin=None) -> Tuple[List[Finding], bool]:
    """(findings ranked most worthwhile first, truncated). With a `twin`,
    unchanged files reuse their cached findings and duplicate windows."""
    from coded.repomap import CODE_SUFFIXES, _readable

    stop = deadline if deadline is not None else time.monotonic() + MAX_SECONDS
    root = Path(cwd)
    code = [f for f in files if Path(f).suffix.lower() in CODE_SUFFIXES]
    if ignored is not None:
        code = [f for f in code if not ignored(root / f)]
    truncated = len(code) > max_files
    code = code[:max_files]
    windows: Dict[str, List[Tuple[int, str]]] = {}
    findings: List[Finding] = []
    for rel in code:
        if time.monotonic() > stop:
            truncated = True
            break
        entry = twin.entry(rel) if twin is not None else None
        if entry is not None and "health" in entry:
            findings += [Finding(**f) for f in entry["health"]]
            windows[rel] = [tuple(w) for w in entry["windows"]]
            continue
        text = _readable(root / rel)
        if text is None:
            continue
        mine = python_findings(rel, text) if rel.endswith(".py") else []
        windows[rel] = _windows(text)
        findings += mine
        if entry is not None:
            entry["health"] = [dict(f.__dict__) for f in mine]  # copies: ranking rescales
            entry["windows"] = [list(w) for w in windows[rel]]
    findings += _duplicates_from(windows)
    try:
        from coded.depgraph import build

        rev = build(cwd, list(windows), deadline=stop + 2, twin=twin).dependents
    except Exception:  # noqa: BLE001 - ranking still works without fan-in
        rev = {}
    for f in findings:
        f.score *= 1 + 0.25 * min(len(rev.get(f.path, ())), 8)
    findings.sort(key=lambda f: (-f.score, f.path, f.line))
    return findings, truncated


def render(findings: List[Finding], budget: int, dependents: Optional[Dict] = None) -> str:
    if not findings:
        return ("No refactoring hot spots: nothing over the thresholds "
                f"(functions > {LONG_FUNCTION} lines, > {MANY_BRANCHES} branches, > "
                f"{DEEP_NESTING} levels, > {MANY_PARAMS} params; {DUP_WINDOW}-line copies).\n")
    counts: Dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.kind] += 1
    head = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    lines = [f"{len(findings)} hot spot(s): {head}. Most worthwhile first "
             "(weighted by how many files import the file):"]
    used = len(lines[0])
    for i, f in enumerate(findings):
        line = "  " + f.render()
        if used + len(line) + 1 > budget:
            lines.append(f"  … {len(findings) - i} more (narrow with `path`)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) + "\n"


def refactor_prompt(target: str = "") -> str:
    """The turn `/refactor [path]` starts (the REPL and the TUI share it)."""
    where = f" in `{target.strip()}`" if target.strip() else ""
    return (f"Use the refactor skill to find code-quality hot spots{where} and improve the "
            "few most worthwhile ones without changing behavior. Start with repo_map "
            f"operation \"health\"{f' and path {target.strip()!r}' if target.strip() else ''}, "
            "check impact before each change, and run the affected tests after each step.")
