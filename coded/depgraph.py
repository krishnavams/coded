"""Module dependency graph: who imports whom, and what a change affects.

Python imports are read with `ast` and resolved to files (absolute, relative
and `src/` layouts); JavaScript/TypeScript relative imports (`import … from
'./x'`, `require('./x')`, `export … from`, `import('./x')`) with conservative
regexes. Imports of packages outside the repository are kept by name only.

Everything is bounded (files, bytes, seconds) and nothing raises: this backs
the `repo_map` tool's `imports`, `dependents`, `impact` and `cycles`
operations, and the verify loop uses `impact` to pick the tests to run first.
"""

from __future__ import annotations

import ast
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MAX_FILES = 3_000
MAX_SECONDS = 6.0
_PY = (".py", ".pyi")
_JS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
_JS_RESOLVE = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".d.ts")
_JS_IMPORT = re.compile(
    r"""(?:\bimport\s+(?:[\w*{}\s,$]+\s+from\s+)?|\bexport\s+[\w*{}\s,$]*\s+from\s+|"""
    r"""\brequire\s*\(\s*|\bimport\s*\(\s*)["']([^"'\n]+)["']""")
_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|"
                        r"\.(test|spec)\.[jt]sx?$")


@dataclass
class DepGraph:
    imports: Dict[str, Set[str]] = field(default_factory=dict)      # file -> files it imports
    external: Dict[str, Set[str]] = field(default_factory=dict)     # file -> outside packages
    # Imports made only inside a function: real dependencies (impact counts
    # them) but they run on call, so they cannot form an import-time cycle.
    lazy: Dict[str, Set[str]] = field(default_factory=dict)
    truncated: bool = False

    @property
    def dependents(self) -> Dict[str, Set[str]]:
        rev: Dict[str, Set[str]] = defaultdict(set)
        for src, targets in self.imports.items():
            for t in targets:
                rev[t].add(src)
        return rev

    @property
    def files(self) -> List[str]:
        return sorted(set(self.imports) | {t for ts in self.imports.values() for t in ts})


def is_test_file(path: str) -> bool:
    return bool(_TEST_FILE.search(path.replace("\\", "/")))


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def _py_module_index(files: Sequence[str]) -> Dict[str, str]:
    """dotted module name -> file, for the repo root and a `src/` layout."""
    index: Dict[str, str] = {}
    for rel in files:
        if not rel.endswith(_PY):
            continue
        parts = list(Path(rel).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        names = [".".join(parts)]
        if parts[0] in ("src", "lib") and len(parts) > 1:
            names.append(".".join(parts[1:]))
        for name in names:
            # A real package beats a same-named stub (.pyi) or a deeper duplicate.
            if name not in index or index[name].endswith(".pyi"):
                index[name] = rel
    return index


def _resolve_py(name: str, index: Dict[str, str]) -> Optional[str]:
    """The repo file for a dotted import, by its longest known prefix."""
    parts = name.split(".")
    while parts:
        hit = index.get(".".join(parts))
        if hit:
            return hit
        parts.pop()
    return None


def _eager_import_nodes(tree: ast.AST) -> Set[int]:
    """ids of import nodes that run when the module is imported (not inside a
    function body)."""
    eager: Set[int] = set()
    stack = list(getattr(tree, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            eager.add(id(node))
        stack.extend(ast.iter_child_nodes(node))
    return eager


def py_raw_imports(rel: str, text: str) -> List[list]:
    """The file's imports before resolution (cacheable): [kind, name, names,
    eager] with relative imports already made absolute. kind is "import" or
    "from"; eager is False for an import inside a function."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    eager_nodes = _eager_import_nodes(tree)
    pkg = list(Path(rel).with_suffix("").parts)[:-1]  # the file's package
    out: List[list] = []
    for node in ast.walk(tree):
        eager = id(node) in eager_nodes
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(["import", alias.name, [], eager])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 > len(pkg):
                    continue
                base = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            if prefix:
                out.append(["from", prefix, [a.name for a in node.names], eager])
    return out


def _py_resolve(rel: str, raw: List[list], index: Dict[str, str]) -> Tuple[Set[str], Set[str], Set[str]]:
    """(local files, outside packages, lazy-only local files) for raw imports."""
    local: Set[str] = set()
    outside: Set[str] = set()
    eager: Set[str] = set()

    def found(path: str, now: bool) -> None:
        local.add(path)
        if now:
            eager.add(path)

    def add(name: str, now: bool) -> None:
        hit = _resolve_py(name, index)
        if hit and hit != rel:
            found(hit, now)
        elif not hit:
            outside.add(name.split(".")[0])

    for kind, name, names, now in raw:
        if kind == "import":
            add(name, now)
            continue
        # `from pkg import mod` may name a submodule rather than a symbol.
        for alias in names:
            sub = f"{name}.{alias}"
            if alias != "*" and sub in index:
                if index[sub] != rel:
                    found(index[sub], now)
                continue
            add(name, now)
    return local, outside, local - eager


def _py_imports(rel: str, text: str, index: Dict[str, str]) -> Tuple[Set[str], Set[str], Set[str]]:
    return _py_resolve(rel, py_raw_imports(rel, text), index)


def _resolve_js(rel: str, spec: str, known: Set[str]) -> Optional[str]:
    base = (Path(rel).parent / spec).as_posix()
    parts: List[str] = []
    for part in base.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    stem = "/".join(parts)
    if stem in known:
        return stem
    for ext in _JS_RESOLVE:
        if stem + ext in known:
            return stem + ext
    for ext in _JS_RESOLVE:
        if f"{stem}/index{ext}" in known:
            return f"{stem}/index{ext}"
    # TypeScript imports name the compiled .js file.
    if stem.endswith(".js"):
        for ext in (".ts", ".tsx"):
            if stem[:-3] + ext in known:
                return stem[:-3] + ext
    return None


def js_raw_imports(text: str) -> List[str]:
    return [m.group(1).strip() for m in _JS_IMPORT.finditer(text)]


def _js_imports(rel: str, text: str, known: Set[str]) -> Tuple[Set[str], Set[str]]:
    return _js_resolve(rel, js_raw_imports(text), known)


def _js_resolve(rel: str, specs: List[str], known: Set[str]) -> Tuple[Set[str], Set[str]]:
    local: Set[str] = set()
    outside: Set[str] = set()
    for spec in specs:
        if spec.startswith("."):
            hit = _resolve_js(rel, spec, known)
            if hit and hit != rel:
                local.add(hit)
        elif spec:
            name = spec.split("/")[0] if not spec.startswith("@") else "/".join(spec.split("/")[:2])
            outside.add(name)
    return local, outside


def build(cwd: str, files: Sequence[str], *, max_files: int = MAX_FILES,
          deadline: Optional[float] = None, ignored=None, twin=None) -> DepGraph:
    """The import graph of `files` (repo-relative). `ignored(path)` drops
    files the user excluded (.codedignore). With a `twin` (twin.py), a file
    whose size and mtime are unchanged is not read or parsed again."""
    from coded.repomap import _readable

    stop = deadline if deadline is not None else time.monotonic() + MAX_SECONDS
    root = Path(cwd)
    code = [f for f in files if f.endswith(_PY + _JS)]
    if ignored is not None:
        code = [f for f in code if not ignored(root / f)]
    graph = DepGraph(truncated=len(code) > max_files)
    code = code[:max_files]
    known = set(code)
    index = _py_module_index(code)
    for rel in code:
        if time.monotonic() > stop:
            graph.truncated = True
            break
        entry = twin.entry(rel) if twin is not None else None
        raw = entry.get("imports") if entry is not None else None
        if raw is None:
            text = _readable(root / rel)
            if text is None:
                continue
            raw = py_raw_imports(rel, text) if rel.endswith(_PY) else js_raw_imports(text)
            if entry is not None:
                entry["imports"] = raw
        lazy: Set[str] = set()
        if rel.endswith(_PY):
            local, outside, lazy = _py_resolve(rel, raw, index)
        else:
            local, outside = _js_resolve(rel, raw, known)
        graph.imports[rel] = local
        if lazy:
            graph.lazy[rel] = lazy
        if outside:
            graph.external[rel] = outside
    return graph


def build_for(cwd: str, twin=None) -> DepGraph:
    """The graph of the whole repository, honouring .codedignore."""
    from coded.repo_overview import list_files

    try:
        files, _ = list_files(cwd)
    except Exception:  # noqa: BLE001 - a broken git must not break the caller
        return DepGraph()
    try:
        from coded.ignore import rules_for

        rules = rules_for(cwd)
        ignored = rules.ignored if rules else None
    except Exception:  # noqa: BLE001
        ignored = None
    return build(cwd, files, ignored=ignored, twin=twin)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------
def impact(graph: DepGraph, changed: Iterable[str], max_depth: int = 6) -> Dict[str, int]:
    """Every file that depends on a changed file, directly (1) or through
    others (2, 3, …), up to `max_depth` steps. Changed files are depth 0."""
    rev = graph.dependents
    seen: Dict[str, int] = {}
    queue: deque = deque()
    for path in changed:
        seen[path] = 0
        queue.append(path)
    while queue:
        cur = queue.popleft()
        if seen[cur] >= max_depth:
            continue
        for dep in sorted(rev.get(cur, ())):
            if dep not in seen:
                seen[dep] = seen[cur] + 1
                queue.append(dep)
    return seen


def affected_tests(graph: DepGraph, changed: Iterable[str]) -> List[str]:
    """The test files a change can break: changed tests and every test that
    (transitively) imports a changed file, nearest first."""
    hits = impact(graph, changed)
    return sorted((p for p in hits if is_test_file(p)), key=lambda p: (hits[p], p))


def cycles(graph: DepGraph, limit: int = 20) -> List[List[str]]:
    """Import-time cycles (strongly connected groups of 2+ files over imports
    that run on import, not inside functions), largest first."""
    edges = {f: t - graph.lazy.get(f, set()) for f, t in graph.imports.items()}
    index_of: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    out: List[List[str]] = []
    counter = [0]

    def strong(v: str) -> None:
        # Iterative Tarjan, so a deep chain cannot hit the recursion limit.
        work = [(v, iter(sorted(edges.get(v, ()))))]
        index_of[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        while work:
            node, it = work[-1]
            pushed = False
            for w in it:
                if w not in index_of:
                    index_of[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    pushed = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index_of[w])
            if pushed:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index_of[node]:
                group = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    group.append(w)
                    if w == node:
                        break
                if len(group) > 1:
                    out.append(sorted(group))

    for v in sorted(graph.imports):
        if v not in index_of:
            strong(v)
    return sorted(out, key=len, reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Rendering (for the repo_map tool)
# ---------------------------------------------------------------------------
def _cap(lines: List[str], budget: int) -> str:
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > budget:
            out.append(f"… {len(lines) - i} more lines (narrow the question)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) + "\n"


def _match(graph: DepGraph, path: str) -> List[str]:
    """Files named by a path argument: the file itself, or everything under a folder."""
    path = path.strip().strip("/").replace("\\", "/")
    files = graph.files
    if path in files:
        return [path]
    return [f for f in files if f.startswith(path + "/")]


def render_imports(graph: DepGraph, path: str, budget: int) -> str:
    targets = _match(graph, path)
    if not targets:
        return ""
    lines = []
    for f in targets:
        local = sorted(graph.imports.get(f, ()))
        outside = sorted(graph.external.get(f, ()))
        lines.append(f"{f} imports:")
        lines += [f"  {t}" for t in local] or ["  (no files in this repository)"]
        if outside:
            lines.append(f"  outside the repo: {', '.join(outside)}")
    return _cap(lines, budget)


def render_dependents(graph: DepGraph, path: str, budget: int) -> str:
    targets = _match(graph, path)
    if not targets:
        return ""
    rev = graph.dependents
    lines = []
    for f in targets:
        users = sorted(rev.get(f, ()))
        lines.append(f"{f} is imported by {len(users)} file(s):")
        lines += [f"  {u}" for u in users] or ["  (nothing in this repository)"]
    return _cap(lines, budget)


def render_impact(graph: DepGraph, paths: Sequence[str], budget: int) -> str:
    changed = [f for p in paths for f in _match(graph, p)]
    if not changed:
        return ""
    hits = impact(graph, changed)
    by_depth: Dict[int, List[str]] = defaultdict(list)
    for f, d in hits.items():
        by_depth[d].append(f)
    tests = affected_tests(graph, changed)
    lines = [f"Changing {', '.join(sorted(set(changed))[:6])}"
             f"{' …' if len(set(changed)) > 6 else ''} can affect {len(hits) - len(changed)} "
             f"other file(s)."]
    for d in sorted(by_depth):
        if d == 0:
            continue
        label = "directly (imports it)" if d == 1 else f"{d} steps away"
        lines.append(f"{label}:")
        lines += [f"  {f}" for f in sorted(by_depth[d])]
    lines.append(f"Tests to run ({len(tests)}):" if tests else
                 "No test file imports these, directly or indirectly.")
    lines += [f"  {t}" for t in tests]
    return _cap(lines, budget)


def render_cycles(graph: DepGraph, budget: int) -> str:
    found = cycles(graph)
    if not found:
        return "No import cycles.\n"
    lines = [f"{len(found)} import cycle(s) (files that import each other, directly or "
             "through others):"]
    for group in found:
        lines.append("  " + " ↔ ".join(group))
    return _cap(lines, budget)
