"""Fetch installable content (skills, plugins, marketplaces) from a source.

Supported sources:
  - a local directory:          ./my-skill   /abs/path/plugin
  - a local or remote .zip:     ./skill.zip  https://example.com/plugin.zip
  - a git URL:                  https://github.com/o/r.git  git@host:o/r.git  file:///repo
  - GitHub shorthand:           owner/repo   owner/repo/sub/path   owner/repo@v1.2
  - a GitHub folder link:       https://github.com/o/r/tree/main/skills/x
  - a skills.sh page:           https://skills.sh/o/r/skill-name (see skills_sh_page)

Everything is fetched into a fresh temporary directory first; callers validate
it there before moving anything into place.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess

from coded.shells import rmtree as _rmtree, text_kwargs
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_ZIP_FILES = 20_000
_GH_SHORTHAND = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?P<path>(/[^@]+)?)(@(?P<ref>.+))?$")


class SourceError(Exception):
    pass


@dataclass
class SourceInfo:
    type: str  # dir | zip | zip-url | git | github
    location: str  # path or URL
    ref: Optional[str] = None
    subpath: Optional[str] = None
    commit: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceInfo":
        return cls(**{k: data.get(k) for k in ("type", "location", "ref", "subpath", "commit")})

    def describe(self) -> str:
        out = self.location
        if self.subpath:
            out += f" ({self.subpath})"
        if self.ref:
            out += f" @ {self.ref}"
        if self.commit:
            out += f" [{self.commit[:10]}]"
        return out


def parse_source(source: str, ref: Optional[str] = None, subpath: Optional[str] = None) -> SourceInfo:
    """Work out what kind of source a user-supplied string is."""
    source = source.strip()
    if not source:
        raise SourceError("Empty source.")
    if source.startswith("-"):
        raise SourceError(f"Invalid source {source!r}.")
    local = Path(source).expanduser()
    if local.exists():
        kind = "zip" if local.is_file() and source.lower().endswith(".zip") else "dir"
        if local.is_file() and kind != "zip":
            raise SourceError(f"{source} is a file; expected a directory or a .zip archive.")
        return SourceInfo(kind, str(local.resolve()), ref, subpath)
    tree = _GH_TREE.match(source)
    if tree:  # a folder link copied from the browser
        return SourceInfo("github", f"https://github.com/{tree.group('owner')}/{tree.group('repo')}.git",
                          ref or tree.group("ref"), subpath or (tree.group("path") or "").strip("/") or None)
    page = skills_sh_page(source)
    if page:
        return parse_source(page[0], ref, subpath)
    if re.match(r"^https?://", source) and source.lower().split("?")[0].endswith(".zip"):
        return SourceInfo("zip-url", source, ref, subpath)
    if re.match(r"^(https?|ssh|git|file)://", source) or source.startswith("git@") or source.endswith(".git"):
        return SourceInfo("git", source, ref, subpath)
    m = _GH_SHORTHAND.match(source)
    if m and not source.startswith(("./", "../", "/")):
        path = (m.group("path") or "").strip("/") or None
        return SourceInfo("github", f"https://github.com/{m.group('owner')}/{m.group('repo')}.git",
                          ref or m.group("ref"), subpath or path)
    raise SourceError(f"Source not found: {source} (not a local path, .zip, git URL, or owner/repo).")


_GH_TREE = re.compile(r"^https?://(www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)"
                      r"/(tree|blob)/(?P<ref>[^/]+)(?P<path>/.*)?$")
_SKILLS_SH = re.compile(r"^(https?://)?(www\.)?skills\.sh/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
                        r"(/(?P<skill>[\w.-]+))?/?(\?.*)?$")


def skills_sh_page(source: str) -> Optional[Tuple[str, Optional[str]]]:
    """A skills.sh page link → (GitHub owner/repo, skill name or None).

    skills.sh is a directory of skills that live in GitHub repositories, so the
    page names the repository (and, for one skill, its name — not a folder)."""
    m = _SKILLS_SH.match(source.strip())
    if not m:
        return None
    return f"{m.group('owner')}/{m.group('repo')}", m.group("skill")


def safe_extract_zip(archive: Path, dest: Path) -> None:
    """Extract a zip, refusing path traversal, symlinks and zip bombs."""
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise SourceError(f"Not a valid zip archive: {exc}") from exc
    with zf:
        members = zf.infolist()
        if len(members) > MAX_ZIP_FILES:
            raise SourceError(f"Archive has too many files ({len(members)}).")
        total = sum(m.file_size for m in members)
        if total > MAX_ZIP_BYTES:
            raise SourceError(f"Archive expands to {total // (1024 * 1024)} MB; limit is "
                              f"{MAX_ZIP_BYTES // (1024 * 1024)} MB.")
        root = dest.resolve()
        for m in members:
            name = m.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
                raise SourceError(f"Unsafe path in archive: {m.filename}")
            mode = (m.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise SourceError(f"Archive contains a symlink: {m.filename}")
            target = (root / name).resolve()
            if root != target and root not in target.parents:
                raise SourceError(f"Unsafe path in archive: {m.filename}")
        for m in members:
            zf.extract(m, root)
            perms = (m.external_attr >> 16) & 0o777
            if perms and not m.is_dir():
                os.chmod(root / m.filename, perms | 0o600)


def _single_top_dir(path: Path) -> Path:
    """Zips often wrap everything in one folder (repo-main/); descend into it."""
    entries = [p for p in path.iterdir() if p.name not in ("__MACOSX",)]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return path


def _git(*args: str, cwd: Optional[str] = None) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=env, **text_kwargs(),
                              timeout=600)
    except FileNotFoundError as exc:
        raise SourceError("git is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceError("git timed out.") from exc
    if proc.returncode != 0:
        raise SourceError((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed")
    return proc.stdout.strip()


def _clone(url: str, ref: Optional[str], dest: Path) -> str:
    if ref:
        try:
            _git("clone", "--quiet", "--depth", "1", "--branch", ref, "--", url, str(dest))
        except SourceError:
            # Not a branch/tag (probably a commit): full clone, then check it out.
            _rmtree(dest)
            _git("clone", "--quiet", "--", url, str(dest))
            _git("checkout", "--quiet", ref, cwd=str(dest))
    else:
        _git("clone", "--quiet", "--depth", "1", "--", url, str(dest))
    return _git("rev-parse", "HEAD", cwd=str(dest))


def _download(url: str, dest: Path) -> None:
    import httpx

    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as resp:
            resp.raise_for_status()
            size = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_ZIP_BYTES:
                        raise SourceError("Download is too large.")
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        raise SourceError(f"Download failed: {exc}") from exc


class Fetched:
    """A fetched source in a temp directory. Use as a context manager."""

    def __init__(self, info: SourceInfo):
        self.info = info
        self._tmp = Path(tempfile.mkdtemp(prefix="coded-fetch-"))
        self.root = self._tmp
        self.links: List[Path] = []  # symlinks removed from the fetched copy

    def __enter__(self) -> "Fetched":
        try:
            self._fetch()
        except OSError as exc:
            self.cleanup()
            raise SourceError(f"Could not read {self.info.location}: {exc}") from exc
        except BaseException:
            self.cleanup()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        _rmtree(self._tmp)

    def _fetch(self) -> None:
        info = self.info
        if info.type == "dir":
            src = Path(info.location)
            dest = self._tmp / "src"
            # Keep links as links (never follow them while copying); they're
            # removed below before anything is read, shown or installed.
            shutil.copytree(src, dest, symlinks=True,
                            ignore=shutil.ignore_patterns(".git", "__pycache__"))
            self.root = dest
        elif info.type in ("zip", "zip-url"):
            archive = Path(info.location)
            if info.type == "zip-url":
                archive = self._tmp / "download.zip"
                _download(info.location, archive)
            dest = self._tmp / "src"
            dest.mkdir()
            safe_extract_zip(archive, dest)
            self.root = _single_top_dir(dest)
        elif info.type in ("git", "github"):
            dest = self._tmp / "src"
            info.commit = _clone(info.location, info.ref, dest)
            _rmtree(dest / ".git")
            self.root = dest
        else:
            raise SourceError(f"Unknown source type {info.type!r}.")
        # Many repositories carry a harmless link at the top (CLAUDE.md ->
        # AGENTS.md), so a link is not a reason to refuse the whole source.
        # Every link is deleted unread; `refuse_links_in` then refuses only an
        # install whose own files contained one.
        self.links = strip_symlinks(self._tmp)
        if info.subpath:
            sub = (self.root / info.subpath).resolve()
            if self.root.resolve() not in sub.parents and sub != self.root.resolve():
                raise SourceError(f"Sub-path escapes the source: {info.subpath}")
            if not sub.is_dir():
                raise SourceError(f"Sub-path not found in source: {info.subpath}")
            self.root = sub


    def refuse_links_in(self, directory: Path) -> None:
        """Refuse to install `directory` if a symlink was removed from inside it."""
        directory = Path(directory)
        for link in self.links:
            if link == directory or directory in link.parents:
                rel = link.relative_to(self.root).as_posix() if self.root in link.parents \
                    else link.name
                raise SourceError(f"Source contains a symlink ({rel}); symlinks are not allowed.")


def strip_symlinks(root: Path) -> List[Path]:
    """Delete every symlink under `root` without following any; return where they were."""
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                found.append(p)
    for p in found:
        p.unlink()
    return found


def reject_symlinks(root: Path) -> None:
    """Refuse sources containing symlinks: a link to ~ or / would otherwise be
    copied (followed) into the install directory."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                rel = p.relative_to(root).as_posix().split("/", 1)[-1]
                raise SourceError(f"Source contains a symlink ({rel}); symlinks are not allowed.")


def install_tree(src: Path, dest: Path) -> None:
    """Copy `src` to `dest`, replacing any existing copy only once the new one is in place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(f".{dest.name}.new")
    backup = dest.with_name(f".{dest.name}.old")
    _rmtree(staging)
    _rmtree(backup)
    shutil.copytree(src, staging, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    if dest.exists():
        dest.rename(backup)
    try:
        staging.rename(dest)
    except OSError:
        if backup.exists():
            backup.rename(dest)
        raise
    _rmtree(backup)
