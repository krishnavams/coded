"""Background shell processes (dev servers, watchers, long builds).

Output goes to a temp file so the agent can poll it incrementally with
``bash_output`` and stop the process with ``kill_shell``. Every process runs in
its own process group (POSIX) or new process group (Windows) so killing it also
kills its children — see ``shells.kill_tree``.
"""

from __future__ import annotations

import atexit
import itertools
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from coded.shells import child_env, command_argv, kill_tree, popen_kwargs


@dataclass
class _Shell:
    id: str
    command: str
    proc: subprocess.Popen
    log_path: str
    offset: int = 0


# New output returned per read: the newest part of a log that may be huge.
BG_READ_MAX_BYTES = 4 * 1024 * 1024


class BackgroundShells:
    def __init__(self) -> None:
        self._shells: Dict[str, _Shell] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        atexit.register(self.kill_all)

    def start(self, argv: Union[str, List[str]], cwd: str, label: str) -> str:
        fd, log_path = tempfile.mkstemp(prefix="coded-bg-", suffix=".log")
        spec = command_argv(argv) if isinstance(argv, str) else argv
        with os.fdopen(fd, "wb") as log:
            proc = subprocess.Popen(
                spec, shell=isinstance(spec, str), cwd=cwd, env=child_env(), stdout=log,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                **popen_kwargs(new_group=True),
            )
        with self._lock:
            shell_id = f"bg{next(self._ids)}"
            self._shells[shell_id] = _Shell(shell_id, label, proc, log_path)
        return shell_id

    def get(self, shell_id: str) -> Optional[_Shell]:
        return self._shells.get(shell_id)

    def read(self, shell_id: str) -> Optional[tuple]:
        """(new output since last read, status string) or None if unknown."""
        sh = self._shells.get(shell_id)
        if sh is None:
            return None
        try:
            with open(sh.log_path, "rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                skipped = max(0, size - sh.offset - BG_READ_MAX_BYTES)
                fh.seek(sh.offset + skipped)
                data = fh.read(BG_READ_MAX_BYTES)
                sh.offset = fh.tell()
                if skipped:
                    data = f"… [{skipped} earlier bytes not shown]\n".encode() + data
        except OSError:
            data = b""
        code = sh.proc.poll()
        status = "running" if code is None else f"exited {code}"
        return data.decode("utf-8", errors="replace"), status

    def wait(self, shell_id: str, timeout: float, cancel=None, step: float = 0.2) -> bool:
        """Block until the shell has new output or has exited, `timeout` passes
        or `cancel` (a threading.Event) is set. False if the shell is unknown."""
        sh = self._shells.get(shell_id)
        if sh is None:
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if sh.proc.poll() is not None or (cancel is not None and cancel.is_set()):
                return True
            try:
                if os.path.getsize(sh.log_path) > sh.offset:
                    return True
            except OSError:
                return True
            time.sleep(min(step, max(0.0, deadline - time.monotonic())))
        return True

    def kill(self, shell_id: str) -> bool:
        sh = self._shells.get(shell_id)
        if sh is None:
            return False
        kill_tree(sh.proc)
        return True

    def list(self) -> List[tuple]:
        out = []
        for sh in self._shells.values():
            code = sh.proc.poll()
            out.append((sh.id, sh.command, "running" if code is None else f"exited {code}"))
        return out

    def kill_all(self) -> None:
        for shell_id in list(self._shells):
            self.kill(shell_id)
        for sh in self._shells.values():
            try:
                sh.proc.wait(timeout=5)  # Windows keeps the log file locked until it exits
            except (subprocess.TimeoutExpired, OSError):
                pass
            try:
                os.unlink(sh.log_path)
            except OSError:
                pass
        self._shells.clear()
