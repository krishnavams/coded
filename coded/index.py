"""Local code index for semantic search.

`build` walks the working tree, splits text files into line-based chunks, embeds
them, and stores the vectors in ``.coded/index/index.json``. `search` embeds a
query and returns the most similar chunks by cosine similarity. Pure-Python math
keeps this dependency-free (fine for small/medium repos).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

_IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".next", ".idea", ".tox", "target", ".coded",
}
_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".swift",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".rb", ".dart", ".scala",
    ".sh", ".sql", ".md", ".rst", ".txt", ".toml", ".yaml", ".yml", ".json",
}
_CHUNK_LINES = 40
_MAX_FILE_BYTES = 400_000


@dataclass
class Chunk:
    path: str
    start: int
    end: int
    text: str
    vec: List[float]


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in _CODE_EXTS:
                yield p


def _chunk_file(path: Path, root: Path) -> List[Chunk]:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return []
        data = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in data[:2048]:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    rel = path.relative_to(root).as_posix()
    chunks = []
    for i in range(0, len(lines), _CHUNK_LINES):
        block = lines[i:i + _CHUNK_LINES]
        text = "\n".join(block).strip()
        if text:
            chunks.append(Chunk(rel, i + 1, i + len(block), text, []))
    return chunks


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class CodeIndex:
    def __init__(self, cwd: str):
        self.cwd = Path(cwd)
        self.path = self.cwd / ".coded" / "index" / "index.json"
        self.chunks: List[Chunk] = []
        self.model: Optional[str] = None

    # -- build / persist ----------------------------------------------------
    def build(self, embed_client, progress: Optional[Callable[[int, int], None]] = None) -> int:
        raw: List[Chunk] = []
        for f in _iter_files(self.cwd):
            raw.extend(_chunk_file(f, self.cwd))
        if not raw:
            self.chunks = []
            self._save(embed_client.model.model)
            return 0
        # Embed in batches.
        batch = 64
        for i in range(0, len(raw), batch):
            group = raw[i:i + batch]
            vecs = embed_client.embed([c.text for c in group])
            for c, v in zip(group, vecs):
                c.vec = v
            if progress:
                progress(min(i + batch, len(raw)), len(raw))
        self.chunks = raw
        self._save(embed_client.model.model)
        return len(raw)

    def _save(self, model_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": model_id,
            "chunks": [
                {"path": c.path, "start": c.start, "end": c.end, "text": c.text, "vec": c.vec}
                for c in self.chunks
            ],
        }
        self.path.write_text(json.dumps(payload))

    def load(self) -> bool:
        if not self.path.is_file():
            return False
        data = json.loads(self.path.read_text())
        self.model = data.get("model")
        self.chunks = [
            Chunk(c["path"], c["start"], c["end"], c["text"], c["vec"])
            for c in data.get("chunks", [])
        ]
        return True

    # -- query --------------------------------------------------------------
    def search(self, embed_client, query: str, k: int = 8) -> List[dict]:
        if not self.chunks:
            return []
        qvec = embed_client.embed([query])[0]
        scored = [(_cosine(qvec, c.vec), c) for c in self.chunks]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {"score": round(s, 4), "path": c.path, "start": c.start, "end": c.end, "text": c.text}
            for s, c in scored[:k]
        ]
