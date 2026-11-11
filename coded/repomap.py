"""A ranked symbol map of the repository.

Grep answers "where does this string appear". It cannot answer "what is this
codebase made of, and which 2% of it matters for my task" — which is exactly
what a model with a small context window has to know before it can act. This
builds that: the definitions in each file, ranked by how much the rest of the
repository depends on them.

Extraction is `ast` for Python and conservative per-language regexes elsewhere
(no tree-sitter, no extra dependency), so it is an outline, not a parse tree:
good enough to point at the right file, never a substitute for reading it.

Ranking is PageRank over a file graph whose edges are "this file names a symbol
that file defines". A widely used module outranks a big unreferenced one, and a
file nobody mentions sinks whatever its size.

Everything is bounded — files scanned, bytes per file, wall time, output — and
nothing here raises: a syntax error, a minified bundle or a binary blob is
skipped, not fatal.
"""

from __future__ import annotations

import ast
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MAX_FILES = 800
MAX_FILE_BYTES = 400_000
MAX_SECONDS = 4.0
MAX_LINE = 2_000  # a longer line means minified or generated: skip the file
_MIN_NAME = 4       # short names collide across unrelated files
_MAX_DEFINERS = 4   # a name defined in many files identifies nothing

CODE_SUFFIXES = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "js", ".tsx": "js",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".java": "java", ".kt": "java", ".kts": "java", ".scala": "java", ".cs": "java",
    ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".hpp": "c", ".cxx": "c",
    ".swift": "swift", ".m": "c", ".lua": "lua", ".sh": "shell", ".bash": "shell",
}

# Deliberately conservative: a missed definition costs a little ranking quality,
# a wrong one pollutes the map.
_PATTERNS: Dict[str, Sequence[Tuple[str, str]]] = {
    "js": (("class", r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"),
           ("func", r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
           ("func", r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
           ("type", r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)")),
    "go": (("func", r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
           ("type", r"^type\s+([A-Za-z_]\w*)")),
    "rust": (("func", r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"),
             ("type", r"^\s*(?:pub\s+)?(?:struct|enum|trait|type)\s+([A-Za-z_]\w*)"),
             ("impl", r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([A-Za-z_]\w*)")),
    "ruby": (("class", r"^\s*(?:class|module)\s+([A-Z]\w*)"),
             ("func", r"^\s*def\s+(?:self\.)?([a-z_]\w*[?!]?)")),
    "php": (("class", r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+([A-Za-z_]\w*)"),
            ("func", r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_]\w*)")),
    "java": (("class", r"^\s*(?:public\s+|private\s+|protected\s+|final\s+|abstract\s+|static\s+|sealed\s+)*"
                       r"(?:class|interface|enum|record|object)\s+([A-Za-z_]\w*)"),
             ("func", r"^\s{2,}(?:public|private|protected|internal|fun|override|suspend)[\w\s<>\[\],.?]*?"
                      r"\b([A-Za-z_]\w*)\s*\(")),
    "c": (("func", r"^[A-Za-z_][\w \t\*&:<>,]*?\b([A-Za-z_]\w*)\s*\([^;]*\)\s*\{"),
          ("type", r"^\s*(?:typedef\s+)?(?:struct|enum|union|class)\s+([A-Za-z_]\w*)")),
    "swift": (("type", r"^\s*(?:public\s+|private\s+|internal\s+|open\s+)?(?:class|struct|enum|protocol|extension)\s+([A-Za-z_]\w*)"),
              ("func", r"^\s*(?:public\s+|private\s+|internal\s+|static\s+|override\s+)*func\s+([A-Za-z_]\w*)")),
    "lua": (("func", r"^\s*(?:local\s+)?function\s+([A-Za-z_][\w.:]*)"),),
    "shell": (("func", r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{"),),
}

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


@dataclass
class Symbol:
    name: str
    kind: str
    line: int

    def render(self) -> str:
        return f"{self.kind} {self.name}"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def extract_symbols(path: str, text: str) -> List[Symbol]:
    """Top-level definitions in one file. Never raises."""
    lang = CODE_SUFFIXES.get(Path(path).suffix.lower())
    if lang is None:
        return []
    try:
        if lang == "python":
            return _python_symbols(text)
        return _regex_symbols(text, _PATTERNS.get(lang, ()))
    except Exception:  # noqa: BLE001 - a map is never worth an exception
        return []


def _python_symbols(text: str) -> List[Symbol]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return _regex_symbols(text, (("class", r"^class\s+(\w+)"),
                                     ("def", r"^(?:async\s+)?def\s+(\w+)")))
    out: List[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.append(Symbol(node.name, "class", node.lineno))
            for child in node.body:  # methods are how a class is actually used
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(Symbol(f"{node.name}.{child.name}", "method", child.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(Symbol(node.name, "def", node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and (t.id.isupper() or t.id[:1].isupper()):
                    out.append(Symbol(t.id, "const", node.lineno))
    return out


def _regex_symbols(text: str, patterns: Sequence[Tuple[str, str]]) -> List[Symbol]:
    if not patterns:
        return []
    compiled = [(kind, re.compile(pat)) for kind, pat in patterns]
    seen: Set[Tuple[str, str]] = set()
    out: List[Symbol] = []
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > MAX_LINE:
            continue
        for kind, rx in compiled:
            m = rx.match(line)
            if m and (kind, m.group(1)) not in seen:
                seen.add((kind, m.group(1)))
                out.append(Symbol(m.group(1), kind, i))
                break
    return out


def _readable(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:4096]:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(len(line) > MAX_LINE for line in text.splitlines()[:200]):
        return None  # minified or generated
    return text


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def pagerank(edges: Dict[str, Dict[str, float]], nodes: Iterable[str],
             damping: float = 0.85, rounds: int = 20) -> Dict[str, float]:
    """Plain PageRank over a weighted directed graph (no numpy)."""
    nodes = list(nodes)
    if not nodes:
        return {}
    rank = {n: 1.0 / len(nodes) for n in nodes}
    out_weight = {n: sum(edges.get(n, {}).values()) for n in nodes}
    for _ in range(rounds):
        nxt = {n: (1.0 - damping) / len(nodes) for n in nodes}
        sunk = 0.0
        for src in nodes:
            targets = edges.get(src) or {}
            if not targets or out_weight[src] <= 0:
                sunk += rank[src]
                continue
            share = damping * rank[src] / out_weight[src]
            for dst, weight in targets.items():
                if dst in nxt:
                    nxt[dst] += share * weight
        if sunk:
            spread = damping * sunk / len(nodes)
            for n in nodes:
                nxt[n] += spread
        rank = nxt
    return rank


@dataclass
class RepoMap:
    files: List[str]                       # ranked, most important first
    symbols: Dict[str, List[Symbol]]
    rank: Dict[str, float]
    scanned: int
    truncated: bool = False


def build(cwd: str, files: Sequence[str], *, max_files: int = MAX_FILES,
          deadline: Optional[float] = None) -> RepoMap:
    """Scan `files` (repo-relative), extract symbols and rank the files."""
    root = Path(cwd)
    stop = (deadline if deadline is not None else time.monotonic() + MAX_SECONDS)
    code = [f for f in files if Path(f).suffix.lower() in CODE_SUFFIXES]
    truncated = len(code) > max_files
    code = code[:max_files]

    symbols: Dict[str, List[Symbol]] = {}
    texts: Dict[str, str] = {}
    for rel in code:
        if time.monotonic() > stop:
            truncated = True
            break
        text = _readable(root / rel)
        if text is None:
            continue
        found = extract_symbols(rel, text)
        if found:
            symbols[rel] = found
        texts[rel] = text

    # symbol name -> files that define it (ambiguous names are weak evidence,
    # so a name defined in many files contributes proportionally less).
    definers: Dict[str, Set[str]] = defaultdict(set)
    for rel, syms in symbols.items():
        for sym in syms:
            # Methods are not evidence of dependency: `run`, `save` and `text`
            # are defined everywhere and would make whichever file holds the most
            # of them look like the heart of the codebase.
            if sym.kind == "method" or "." in sym.name:
                continue
            if len(sym.name) >= _MIN_NAME:
                definers[sym.name].add(rel)
    # A name defined in many places identifies nothing.
    definers = {name: files for name, files in definers.items() if len(files) <= _MAX_DEFINERS}

    edges: Dict[str, Dict[str, float]] = defaultdict(dict)
    for rel, text in texts.items():
        if time.monotonic() > stop:
            truncated = True
            break
        used: Set[str] = set()
        for word in _WORD.finditer(text):
            name = word.group(0)
            if name in definers:
                used.add(name)
        for name in used:
            targets = definers[name]
            weight = 1.0 / len(targets)
            for target in targets:
                if target != rel:
                    edges[rel][target] = edges[rel].get(target, 0.0) + weight

    rank = pagerank(edges, symbols.keys() or texts.keys())
    ordered = sorted(symbols, key=lambda f: (-rank.get(f, 0.0), f))
    return RepoMap(ordered, symbols, rank, scanned=len(texts), truncated=truncated)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(repo_map: RepoMap, budget: int, *, per_file: int = 12,
           names_only: bool = False) -> str:
    """The map as text, largest-first, stopping at `budget` characters."""
    if budget <= 0 or not repo_map.files:
        return ""
    head = (f"# Repository map ({repo_map.scanned} files scanned, ranked by how much "
            f"the rest of the code refers to them)\n"
            "Definitions only — an outline, not the code. Read a file before relying on it.\n")
    lines: List[str] = []
    used = len(head)
    for rel in repo_map.files:
        syms = repo_map.symbols.get(rel) or []
        if len(syms) > per_file:
            # Under a tight budget the top-level names are what locate a file;
            # methods are detail. Keep source order within each group.
            top = [s for s in syms if s.kind != "method"]
            shown = (top + [s for s in syms if s.kind == "method"])[:per_file]
            shown.sort(key=lambda s: s.line)
        else:
            shown = syms
        names = ", ".join(s.name if names_only else s.render() for s in shown)
        if len(syms) > len(shown):
            names += f", … (+{len(syms) - len(shown)})"
        entry = f"\n{rel}\n  {names}\n" if names else f"\n{rel}\n"
        if used + len(entry) > budget:
            lines.append(f"\n… ({len(repo_map.files) - len(lines)} more files omitted)\n")
            break
        lines.append(entry)
        used += len(entry)
    return head + "".join(lines)


def build_repo_map(cwd: str, budget: int, *, subtree: Optional[str] = None,
                   focus: Optional[str] = None, per_file: int = 12) -> str:
    """The whole thing: list files, scan, rank, render. Returns "" when empty."""
    from coded.repo_overview import list_files

    if budget <= 0:
        return ""
    files, _ = list_files(cwd)
    if subtree:
        prefix = subtree.strip("/") + "/"
        files = [f for f in files if f == subtree.strip("/") or f.startswith(prefix)]
    repo_map = build(cwd, files)
    if focus:
        repo_map = _refocus(repo_map, focus)
    return render(repo_map, budget, per_file=per_file)


def _refocus(repo_map: RepoMap, focus: str) -> RepoMap:
    """Put the files that define or mention `focus` first, keeping the rest."""
    needle = focus.rsplit(".", 1)[-1].lower()
    def score(rel: str) -> tuple:
        syms = repo_map.symbols.get(rel) or []
        exact = any(s.name.lower() == needle or s.name.lower().endswith("." + needle)
                    for s in syms)
        partial = any(needle in s.name.lower() for s in syms)
        in_path = needle in rel.lower()
        return (not exact, not partial, not in_path, -repo_map.rank.get(rel, 0.0), rel)
    return RepoMap(sorted(repo_map.files, key=score), repo_map.symbols, repo_map.rank,
                   repo_map.scanned, repo_map.truncated)
