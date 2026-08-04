"""Checkpoint / undo for file mutations.

Before a mutating tool (write/edit/move/delete) changes a file, it records the
file's prior state into a checkpoint *group* (one group per user turn). `/undo`
restores the most recent group: files that existed are rewritten with their old
contents, and files that were newly created are removed.

Groups persist under ``.coded/checkpoints/`` so undo survives a restart. Note:
changes made by the `bash` tool are **not** tracked (coded can't know which
files a shell command touched).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class _Group:
    id: str
    label: str
    directory: Path
    records: Dict[str, dict] = field(default_factory=dict)  # abs path -> record
    counter: int = 0


class CheckpointManager:
    def __init__(self, cwd: str):
        self.cwd = Path(cwd)
        self.root = self.cwd / ".coded" / "checkpoints"
        self.current: Optional[_Group] = None
        # Monotonic sequence so group ids sort chronologically even within the
        # same millisecond; continue from any groups already on disk.
        self._seq = len(self._group_dirs())

    # -- recording ----------------------------------------------------------
    def begin(self, label: str) -> None:
        self._seq += 1
        gid = time.strftime("%Y%m%d-%H%M%S") + f"-{self._seq:06d}"
        self.current = _Group(id=gid, label=label, directory=self.root / gid)

    def record(self, path: Path) -> None:
        """Snapshot a file's current state before it is modified."""
        if self.current is None:
            self.begin("auto")
        group = self.current
        assert group is not None
        key = str(path.resolve())
        if key in group.records:
            return
        group.directory.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_file():
            backup = f"{group.counter}.bak"
            group.counter += 1
            try:
                (group.directory / backup).write_bytes(path.read_bytes())
            except OSError:
                return
            group.records[key] = {"path": key, "existed": True, "backup": backup}
        else:
            group.records[key] = {"path": key, "existed": False, "backup": None}
        self._write_manifest(group)

    def _write_manifest(self, group: _Group) -> None:
        manifest = {
            "id": group.id,
            "label": group.label,
            "created": time.time(),
            "files": list(group.records.values()),
        }
        try:
            (group.directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
        except OSError:
            pass

    # -- listing / undo -----------------------------------------------------
    def _group_dirs(self) -> List[Path]:
        if not self.root.is_dir():
            return []
        return sorted((d for d in self.root.iterdir() if (d / "manifest.json").is_file()),
                      key=lambda d: d.name)

    def list_groups(self) -> List[dict]:
        out = []
        for d in self._group_dirs():
            try:
                out.append(json.loads((d / "manifest.json").read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def undo_last(self) -> str:
        dirs = self._group_dirs()
        if not dirs:
            return "Nothing to undo."
        group_dir = dirs[-1]
        try:
            manifest = json.loads((group_dir / "manifest.json").read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return f"Could not read checkpoint: {exc}"

        restored, removed = [], []
        for rec in manifest.get("files", []):
            p = Path(rec["path"])
            if rec.get("existed"):
                backup = group_dir / rec["backup"]
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(backup.read_bytes())
                    restored.append(str(p))
                except OSError:
                    pass
            else:
                # File was newly created by the recorded action → remove it.
                try:
                    if p.exists():
                        p.unlink()
                        removed.append(str(p))
                except OSError:
                    pass

        # Consume the group so a second /undo steps further back.
        try:
            for f in group_dir.iterdir():
                f.unlink()
            group_dir.rmdir()
        except OSError:
            pass
        if self.current and self.current.directory == group_dir:
            self.current = None

        parts = []
        if restored:
            parts.append(f"restored {len(restored)} file(s)")
        if removed:
            parts.append(f"removed {len(removed)} newly-created file(s)")
        label = manifest.get("label", "")
        return f"Undid checkpoint '{label}': " + (", ".join(parts) or "no changes") + "."
