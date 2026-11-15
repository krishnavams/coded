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


# ---------------------------------------------------------------------------
# One process per session: a lock file next to the session
# ---------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    """Whether a process with this id runs (never signals it: on Windows
    os.kill(pid, 0) would terminate it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


class Busy(RuntimeError):
    """The thing is in use by another live process (or turn)."""

    def __init__(self, what: str, holder: dict):
        self.holder = holder
        pid = holder.get("pid")
        super().__init__(f"{what} is in use by another coded process"
                         + (f" (pid {pid})" if pid else ""))


class FileLock:
    """An exclusive, process-wide claim on `path` (a lock file holding the
    owner's pid and host). A lock whose owner on this host is gone is taken
    over; one held by a live process raises Busy. Released by release(), on
    `with` exit (exceptions too) and at interpreter exit."""

    def __init__(self, path: Path, what: str = "this session"):
        self.path = Path(path)
        self.what = what
        self.held = False

    def holder(self) -> dict:
        try:
            import json

            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _stale(self, info: dict) -> bool:
        import socket

        if not info:
            # unreadable/partial: stale once it is older than a few seconds
            try:
                import time

                return time.time() - self.path.stat().st_mtime > 5
            except OSError:
                return True
        if info.get("host") != socket.gethostname():
            return False  # another machine (shared folder): cannot tell; respect it
        return not pid_alive(int(info.get("pid") or 0))

    def acquire(self) -> "FileLock":
        import json
        import socket
        import time

        if self.held:
            return self
        private_dir(self.path.parent)
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                info = self.holder()
                if info.get("pid") == os.getpid() and info.get("host") == socket.gethostname():
                    # our own process already holds it (another keeper object)
                    raise Busy(self.what, info)
                if self._stale(info):
                    try:
                        os.unlink(self.path)
                    except FileNotFoundError:
                        pass
                    continue
                raise Busy(self.what, info)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "host": socket.gethostname(),
                           "since": time.time()}, fh)
            self.held = True
            import atexit

            atexit.register(self.release)
            return self
        raise Busy(self.what, self.holder())

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            if self.holder().get("pid") == os.getpid():
                os.unlink(self.path)
        except OSError:
            pass

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
