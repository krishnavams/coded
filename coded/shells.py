"""Running shell commands on Linux, macOS and Windows.

POSIX keeps coded's historical behaviour exactly: the command string is handed
to ``subprocess`` with ``shell=True`` (so ``/bin/sh -c``), and a process group
is used for background jobs.

Windows has no POSIX shell, so a shell is resolved once per process:

1. ``"shell"`` in config.json, or ``$CODED_SHELL`` — a name (``bash``, ``pwsh``,
   ``powershell``, ``cmd``) or a full path to a shell executable;
2. **Git Bash** (``bash.exe`` shipped with Git for Windows), found next to
   ``git.exe`` or in the usual install locations — the only option that
   understands the POSIX syntax models write (``&&``, ``$(…)``, quoting);
3. **PowerShell** (``pwsh``, then ``powershell``), driven with
   ``-EncodedCommand`` so quoting can't be mangled;
4. ``cmd.exe``.

``bash.exe`` in ``System32`` is the WSL launcher, not a shell for the current
directory, so it is skipped. The resolved shell is named in the system prompt
(see ``prompts._environment``) so the model writes syntax that actually runs.
"""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

IS_WINDOWS = os.name == "nt"
_TREE_KILL_TIMEOUT = 10


@dataclass(frozen=True)
class Shell:
    """How to run a command string on this machine."""

    name: str              # "sh", "bash", "pwsh", "powershell" or "cmd"
    path: str = ""         # executable (empty on POSIX: the command goes to shell=True)
    posix_syntax: bool = True

    @property
    def label(self) -> str:
        """How the shell is described to the model."""
        if self.name == "sh":
            return "POSIX shell (sh)"
        if self.name == "bash":
            win = self.path.lower().endswith(".exe")
            return f"{'Git Bash' if win else 'bash'} (POSIX syntax)"
        if self.name in ("pwsh", "powershell"):
            return f"PowerShell ({self.name}; PowerShell syntax, not POSIX)"
        return "cmd.exe (Windows command prompt syntax, not POSIX)"


POSIX_SHELL = Shell(name="sh")

_WHICH = Callable[[str], Optional[str]]
_GIT_BASH_GUESSES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
)


def _is_wsl_launcher(path: str) -> bool:
    """System32\\bash.exe launches WSL, which has its own filesystem view."""
    parts = [p.lower() for p in PureWindowsPath(path).parts]
    return "system32" in parts or "sysnative" in parts


def _git_bash(which: _WHICH, exists: Callable[[str], bool]) -> Optional[str]:
    git = which("git")
    if git:
        # ...\Git\cmd\git.exe or ...\Git\bin\git.exe -> ...\Git\bin\bash.exe
        root = PureWindowsPath(git).parent.parent
        for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if exists(str(candidate)):
                return str(candidate)
    found = which("bash")
    if found and not _is_wsl_launcher(found):
        return found
    for guess in _GIT_BASH_GUESSES:
        if exists(guess):
            return guess
    return None


def resolve_shell(configured: Optional[str] = None, *, windows: bool = IS_WINDOWS,
                  which: Optional[_WHICH] = None,
                  exists: Optional[Callable[[str], bool]] = None,
                  env: Optional[Dict[str, str]] = None) -> Shell:
    """Pick the shell for this machine (pure: pass `which`/`exists`/`env` to test)."""
    which = which or shutil.which
    exists = exists or os.path.isfile
    env = os.environ if env is None else env
    if not windows:
        return POSIX_SHELL
    choice = configured or env.get("CODED_SHELL") or ""
    if choice:
        name = PureWindowsPath(choice).stem.lower() or choice.lower()
        is_path = "\\" in choice or "/" in choice or ":" in choice
        path = choice if is_path else (which(choice) or "")
        if not path and name == "bash":
            path = _git_bash(which, exists) or ""
        if not path:
            path = which(name) or which(name + ".exe") or ""
        if path:
            known = name if name in ("bash", "pwsh", "powershell", "cmd") else "cmd"
            return Shell(name=known, path=path, posix_syntax=known == "bash")
    bash = _git_bash(which, exists)
    if bash:
        return Shell(name="bash", path=bash, posix_syntax=True)
    for name in ("pwsh", "powershell"):
        found = which(name)
        if found:
            return Shell(name=name, path=found, posix_syntax=False)
    return Shell(name="cmd", path=which("cmd") or "cmd.exe", posix_syntax=False)


_shell: Optional[Shell] = None
_pinned = False


def active_shell(config: Any = None) -> Shell:
    """The resolved shell, worked out once per process."""
    global _shell
    if _shell is None:
        _shell = resolve_shell(getattr(config, "shell", None))
    return _shell


def set_active_shell(shell: Optional[Shell], pinned: bool = False) -> None:
    """Set the shell for this process. `pinned` (used by --shell) means a later
    `ensure_shell` from config.json must not replace it."""
    global _shell, _pinned
    _shell = shell
    _pinned = pinned and shell is not None


def ensure_shell(configured: Optional[str]) -> Shell:
    """Apply the `shell` setting from config.json, unless --shell pinned one.

    Called by `load_config`, so the setting is in force before anything that
    depends on it (plugin hook paths, the system prompt, the bash tool).
    """
    global _shell
    if not _pinned and configured:
        _shell = resolve_shell(configured)
    return active_shell()


def _powershell_script(command: str) -> str:
    """Run the command, then exit with its status (PowerShell swallows it otherwise).

    Windows PowerShell 5.1 reads a native command's output using the console's
    ANSI code page, so UTF-8 output arrives as mojibake unless both encodings
    are set first.
    """
    return (
        "$ErrorActionPreference = 'Continue'\n"
        "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$OutputEncoding = [Text.Encoding]::UTF8 } catch {}\n"
        f"{command}\n"
        "if ($LASTEXITCODE -ne $null) { exit $LASTEXITCODE }\n"
        "if (-not $?) { exit 1 }\n"
        "exit 0\n"
    )


def command_argv(command: str, shell: Optional[Shell] = None) -> Union[str, List[str]]:
    """The command as subprocess should receive it.

    POSIX: the string itself (callers pass ``shell=True``, as coded always has).
    Windows: a full argv list, so no quoting is left to the shell.
    """
    shell = shell or active_shell()
    if shell.name == "sh":
        return command
    if shell.name == "bash":
        return [shell.path, "-c", command]
    if shell.name in ("pwsh", "powershell"):
        encoded = base64.b64encode(_powershell_script(command).encode("utf-16-le")).decode()
        return [shell.path, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    # cmd /s /c keeps the command string intact after stripping its outer quotes.
    return [shell.path or "cmd.exe", "/d", "/s", "/c", command]


def popen_kwargs(new_group: bool = False) -> Dict[str, Any]:
    """Platform flags so a command's children can be killed with it."""
    if not new_group:
        return {}
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def text_kwargs() -> Dict[str, Any]:
    """Decode child output as UTF-8 everywhere (Windows would use the ANSI code page)."""
    return {"text": True, "encoding": "utf-8", "errors": "replace"}


def child_env(env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    """Environment for a child process. On Windows, ask Python children to write
    UTF-8: the default is the ANSI code page, so printing non-ASCII to a pipe
    raises UnicodeEncodeError in the child."""
    if not IS_WINDOWS:
        return env
    base = dict(env if env is not None else os.environ)
    base.setdefault("PYTHONIOENCODING", "utf-8")
    return base


def kill_tree(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Stop a process and its children, on any platform."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=_TREE_KILL_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        if proc.poll() is None:
            proc.kill()
        return
    import signal

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def run(command: Union[str, Sequence[str]], *, cwd: Optional[str] = None,
        timeout: Optional[float] = None, env: Optional[Dict[str, str]] = None,
        input: Optional[str] = None, shell: Optional[Shell] = None) -> subprocess.CompletedProcess:
    """Run a shell command and capture its output as UTF-8 text.

    `command` is a command string (run through the platform's shell) or an argv
    list (run directly). A timed-out command takes its children with it; on
    Windows that needs an explicit tree kill, since terminating the shell alone
    leaves grandchildren holding the pipes open.
    """
    if isinstance(command, str):
        argv: Union[str, List[str]] = command_argv(command, shell)
    else:
        argv = list(command)
    use_shell = isinstance(argv, str)
    # A child never reads the user's terminal: it would fight the REPL for input
    # and an interactive command would hang until the timeout.
    stdin = subprocess.PIPE if input is not None else subprocess.DEVNULL
    if not IS_WINDOWS:
        if input is not None:
            return subprocess.run(argv, shell=use_shell, cwd=cwd, env=env, input=input,
                                  capture_output=True, timeout=timeout, **text_kwargs())
        return subprocess.run(argv, shell=use_shell, cwd=cwd, env=env, stdin=stdin,
                              capture_output=True, timeout=timeout, **text_kwargs())
    proc = subprocess.Popen(
        argv, shell=use_shell, cwd=cwd, env=child_env(env), stdin=stdin,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **text_kwargs(), **popen_kwargs(new_group=True))
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except BaseException as exc:  # timeout, Ctrl-C, anything: never leave a tree behind
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=_TREE_KILL_TIMEOUT)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            out = err = ""
        if isinstance(exc, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(argv, timeout or 0, output=out, stderr=err) from None
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _drop_readonly(func, path, _exc) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree(path: Union[str, Path]) -> None:
    """Delete a tree, clearing the read-only bit Windows puts on git objects."""
    kwargs: Dict[str, Any] = {"onexc": _drop_readonly} if sys.version_info >= (3, 12) \
        else {"onerror": _drop_readonly}
    try:
        shutil.rmtree(path, **kwargs)
    except (OSError, TypeError):
        shutil.rmtree(path, ignore_errors=True)


def configure_stdio() -> None:
    """Make sure the terminal can print coded's output (box drawing, ●, ⎿, …).

    Windows consoles and redirected pipes often default to a legacy code page,
    where printing the UI raises UnicodeEncodeError.
    """
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") == "utf8":
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def shell_path(path: Union[str, Path], shell: Optional[Shell] = None) -> str:
    r"""A path as it should appear inside a command string for the active shell.

    Git Bash treats ``\`` as an escape, so Windows paths are written with
    forward slashes (``C:/Users/...``), which Git Bash and Windows both accept.
    """
    text = str(path)
    shell = shell or active_shell()
    return text.replace("\\", "/") if shell.posix_syntax and IS_WINDOWS else text


def shell_summary(shell: Optional[Shell] = None) -> Tuple[str, bool]:
    """(label, posix_syntax) for the active shell — used by the system prompt."""
    shell = shell or active_shell()
    return shell.label, shell.posix_syntax
