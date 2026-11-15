"""Private files for what coded keeps about a person's work.

Sessions hold whole conversations: code, file contents, command output and
sometimes secrets a tool printed. On a shared machine the default umask
(0644 files, 0755 folders) would let every other account read them. These
helpers create the folder 0700 and every file 0600 *before* any byte is
written (the temp file is opened with that mode, then atomically renamed),
refuse to write through a symlink, and tighten files an older version left
readable.

Windows: POSIX modes do not apply. The files live under the user's profile
(`%APPDATA%`/`~`), whose ACL already limits access to that account; keep
`CODED_SESSIONS_DIR` inside the profile on shared Windows machines.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Set

_POSIX = os.name == "posix"
_tightened: Set[str] = set()

# A stored id names a file: letters, digits, dot, dash, underscore; no path.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class UnsafePath(OSError):
    """A private path is a symlink, or an id would leave its folder."""


def check_id(value: str) -> str:
    """`value` if it is safe as a file name inside a store, else UnsafePath."""
    value = str(value or "")
    if not _SAFE_ID.match(value) or ".." in value:
        raise UnsafePath(f"not a valid id: {value!r}")
    return value


def private_dir(path: Path) -> Path:
    """Create `path` (and parents) for private data: 0700, not a symlink.
    An existing folder of ours that is readable by others is tightened, and
    files in it are tightened to 0600 (once per process)."""
    path = Path(path)
    if path.is_symlink():
        raise UnsafePath(f"{path} is a symlink; refusing to keep private data there")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _POSIX:
        key = str(path)
        try:
            st = os.stat(path)
            if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & 0o077:
                os.chmod(path, 0o700)
            if key not in _tightened:
                _tightened.add(key)
                for child in path.iterdir():
                    try:
                        cst = os.lstat(child)
                    except OSError:
                        continue
                    if (stat.S_ISREG(cst.st_mode) and cst.st_uid == os.getuid()
                            and stat.S_IMODE(cst.st_mode) & 0o077):
                        os.chmod(child, 0o600)
        except OSError:
            pass
    return path


def write_private(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Atomically replace `path` with `text`, readable by this account only.
    The temp file is created exclusively with mode 0600 (never through a
    symlink), written, flushed and renamed over the target."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    if sys.platform == "win32":
        flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode(encoding))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_private(path: Path, encoding: str = "utf-8") -> str:
    """Read a private file; a symlink in its place is refused."""
    path = Path(path)
    if path.is_symlink():
        raise UnsafePath(f"{path} is a symlink; refusing to read it as private data")
    return path.read_text(encoding=encoding)
