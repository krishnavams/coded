"""Cross-platform support: shell resolution, process control, line endings.

The Windows logic is written as pure functions (`resolve_shell`, `command_argv`,
`popen_kwargs`) that take `which`/`exists`/`env`, so it is exercised on any
platform. Behaviour that can only be observed on Windows itself (taskkill, the
console code page) is covered by the CI matrix, not here.
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from coded import shells, textio
from coded.shells import (
    Shell,
    command_argv,
    popen_kwargs,
    resolve_shell,
    text_kwargs,
)

GIT_CMD = r"C:\Program Files\Git\cmd\git.exe"
GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"


def _which(mapping):
    return lambda name: mapping.get(name)


def _exists(paths):
    return lambda p: p in set(paths)


# ---------------------------------------------------------------------------
# shell resolution
# ---------------------------------------------------------------------------
def test_posix_is_unchanged():
    shell = resolve_shell(windows=False, which=_which({}), exists=_exists([]), env={})
    assert shell.name == "sh" and shell.posix_syntax
    # The command string still goes to subprocess as-is (shell=True, /bin/sh -c).
    assert command_argv("ls -la && echo $HOME", shell) == "ls -la && echo $HOME"
    expected = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
        else {"start_new_session": True}
    assert popen_kwargs(new_group=True) == expected


def test_windows_prefers_git_bash_found_next_to_git():
    shell = resolve_shell(windows=True, which=_which({"git": GIT_CMD}),
                          exists=_exists([GIT_BASH]), env={})
    assert shell.name == "bash" and shell.path == GIT_BASH and shell.posix_syntax
    assert command_argv("pytest -q && echo done", shell) == [GIT_BASH, "-c",
                                                             "pytest -q && echo done"]
    assert "POSIX" in shell.label


def test_windows_skips_the_wsl_bash_launcher():
    wsl = r"C:\Windows\System32\bash.exe"
    shell = resolve_shell(windows=True, which=_which({"bash": wsl, "powershell": "powershell.exe"}),
                          exists=_exists([wsl]), env={})
    assert shell.name == "powershell" and shell.path == "powershell.exe"


def test_windows_falls_back_to_powershell_then_cmd():
    pwsh = resolve_shell(windows=True, which=_which({"pwsh": "pwsh.exe",
                                                     "powershell": "powershell.exe"}),
                         exists=_exists([]), env={})
    assert pwsh.name == "pwsh" and not pwsh.posix_syntax
    cmd = resolve_shell(windows=True, which=_which({"cmd": r"C:\Windows\System32\cmd.exe"}),
                        exists=_exists([]), env={})
    assert cmd.name == "cmd"
    assert command_argv("dir", cmd)[:4] == [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"]


def test_windows_shell_can_be_configured_and_set_by_env():
    which = _which({"git": GIT_CMD, "pwsh": "pwsh.exe"})
    exists = _exists([GIT_BASH])
    assert resolve_shell("pwsh", windows=True, which=which, exists=exists, env={}).name == "pwsh"
    assert resolve_shell(None, windows=True, which=which, exists=exists,
                         env={"CODED_SHELL": "pwsh"}).name == "pwsh"
    # An explicit path is used as given.
    custom = r"D:\tools\busybox\bash.exe"
    picked = resolve_shell(custom, windows=True, which=_which({}), exists=_exists([custom]), env={})
    assert picked.path == custom and picked.name == "bash"
    # Config beats the environment variable.
    assert resolve_shell("cmd", windows=True, which=_which({"cmd": "cmd.exe", "pwsh": "pwsh.exe"}),
                         exists=exists, env={"CODED_SHELL": "pwsh"}).name == "cmd"


def test_powershell_commands_are_encoded_and_return_the_exit_code():
    shell = Shell(name="pwsh", path="pwsh.exe", posix_syntax=False)
    argv = command_argv("python -m pytest", shell)
    assert argv[:5] == ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    script = base64.b64decode(argv[5]).decode("utf-16-le")
    assert "python -m pytest" in script
    assert "exit $LASTEXITCODE" in script and "if (-not $?) { exit 1 }" in script


def test_windows_popen_and_text_settings():
    assert text_kwargs() == {"text": True, "encoding": "utf-8", "errors": "replace"}
    flags = popen_kwargs(new_group=True)
    assert flags  # a new group on either platform, so children can be killed together
    assert popen_kwargs() == {}


# ---------------------------------------------------------------------------
# line endings
# ---------------------------------------------------------------------------
def test_text_io_round_trips_both_line_endings(tmp_path):
    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(b"a\r\nb\r\nc\r\n")
    text, newline = textio.read_text(crlf)
    assert text == "a\nb\nc\n" and newline == "\r\n"
    textio.write_text(crlf, text.replace("b", "B"), newline)
    assert crlf.read_bytes() == b"a\r\nB\r\nc\r\n"

    lf = tmp_path / "lf.txt"
    lf.write_bytes(b"a\nb\n")
    text, newline = textio.read_text(lf)
    assert newline == "\n"
    textio.write_text(lf, text, newline)
    assert lf.read_bytes() == b"a\nb\n"
    assert textio.newline_of(tmp_path / "missing.txt") == "\n"


def test_edit_tools_keep_the_file_s_line_endings(tmp_path):
    from coded.tools.base import ToolContext
    from coded.tools.files import EditTool, WriteTool
    from coded.tools.patch import MultiEditTool

    ctx = ToolContext(cwd=str(tmp_path), permissions=None, config=None)
    for name, tool, args in (
        ("edit.txt", EditTool(), {"old_string": "two", "new_string": "2"}),
        ("multi.txt", MultiEditTool(), {"edits": [{"old_string": "two", "new_string": "2"}]}),
        ("write.txt", WriteTool(), {"content": "one\n2\nthree\n"}),
    ):
        p = tmp_path / name
        p.write_bytes(b"one\r\ntwo\r\nthree\r\n")
        result = tool.run({"path": name, **args}, ctx)
        assert not result.is_error, result.content
        # Only the edited line changed; the file is still CRLF throughout.
        assert p.read_bytes() == b"one\r\n2\r\nthree\r\n"

    lf = tmp_path / "keep.txt"
    lf.write_bytes(b"one\ntwo\n")
    EditTool().run({"path": "keep.txt", "old_string": "two", "new_string": "2"}, ctx)
    assert lf.read_bytes() == b"one\n2\n"


# ---------------------------------------------------------------------------
# the rest of the platform surface
# ---------------------------------------------------------------------------
def test_system_prompt_names_the_shell_for_the_model():
    from coded.prompts import build_system_prompt

    try:
        shells.set_active_shell(Shell(name="cmd", path="cmd.exe", posix_syntax=False))
        prompt = build_system_prompt(".")
        assert "cmd.exe" in prompt and "not POSIX" in prompt
        shells.set_active_shell(Shell(name="bash", path=GIT_BASH))
        prompt = build_system_prompt(".")
        assert "Shell for the bash tool" in prompt and "not POSIX" not in prompt
    finally:
        shells.set_active_shell(None)


def test_external_agent_argv_resolves_shims(monkeypatch):
    from coded.external_agents import resolve_argv

    monkeypatch.setattr("shutil.which", lambda name: r"C:\npm\claude.cmd" if name == "claude" else None)
    assert resolve_argv(["claude", "-p", "hi"]) == [r"C:\npm\claude.cmd", "-p", "hi"]
    assert resolve_argv(["nope", "-p"]) == ["nope", "-p"]
    assert resolve_argv([]) == []


def test_rmtree_removes_read_only_files(tmp_path):
    d = tmp_path / "repo"
    (d / "objects").mkdir(parents=True)
    f = d / "objects" / "pack"
    f.write_text("x")
    os.chmod(f, stat.S_IRUSR)
    shells.rmtree(d)
    assert not d.exists()
    shells.rmtree(tmp_path / "gone")  # missing paths are fine


def test_run_captures_utf8_and_times_out(tmp_path):
    out = shells.run([sys.executable, "-c", "print('héllo ✓')"], cwd=str(tmp_path), timeout=60)
    assert out.returncode == 0 and "héllo ✓" in out.stdout
    with pytest.raises(subprocess.TimeoutExpired):
        shells.run([sys.executable, "-c", "import time; time.sleep(30)"],
                   cwd=str(tmp_path), timeout=1)


def test_configure_stdio_is_safe_to_call():
    shells.configure_stdio()
    assert sys.stdout is not None


def test_non_utf8_files_are_refused_not_corrupted(tmp_path):
    from coded.tools.base import ToolContext
    from coded.tools.files import EditTool
    from coded.tools.patch import ApplyPatchTool, MultiEditTool

    ctx = ToolContext(cwd=str(tmp_path), permissions=None, config=None)
    raw = "café\r\nsecond\r\n".encode("cp1252")
    for name, tool, args in (
        ("a.txt", EditTool(), {"old_string": "second", "new_string": "2"}),
        ("b.txt", MultiEditTool(), {"edits": [{"old_string": "second", "new_string": "2"}]}),
    ):
        (tmp_path / name).write_bytes(raw)
        result = tool.run({"path": name, **args}, ctx)
        assert result.is_error and "not valid UTF-8" in result.content
        assert (tmp_path / name).read_bytes() == raw  # untouched
        assert tool.preview({"path": name, **args}, ctx) is None

    (tmp_path / "c.txt").write_bytes(raw)
    patch = "*** Begin Patch\n*** Update File: c.txt\n@@\n-second\n+2\n*** End Patch"
    result = ApplyPatchTool().run({"patch": patch}, ctx)
    assert result.is_error and "not valid UTF-8" in result.content
    assert (tmp_path / "c.txt").read_bytes() == raw


# ---------------------------------------------------------------------------
# Real shells. These only run on Windows (CI matrix); elsewhere they skip.
# ---------------------------------------------------------------------------
windows_only = pytest.mark.skipif(os.name != "nt", reason="needs a Windows shell")


def _available(name):
    import shutil as _shutil

    path = _shutil.which(name)
    if not path:
        pytest.skip(f"{name} is not installed")
    return path


@windows_only
@pytest.mark.parametrize("name", ["pwsh", "powershell", "cmd"])
def test_real_windows_shell_exit_codes_and_output(name, tmp_path):
    shell = resolve_shell(_available(name), windows=True)
    assert shell.name == name

    ok = shells.run("echo hello", cwd=str(tmp_path), timeout=120, shell=shell)
    assert ok.returncode == 0 and "hello" in ok.stdout

    # An explicit exit code survives the shell wrapper.
    code = "exit 3" if name == "cmd" else "exit 3"
    assert shells.run(code, cwd=str(tmp_path), timeout=120, shell=shell).returncode == 3

    # A failing command reports failure, not success.
    bad = shells.run("this-command-does-not-exist-xyz", cwd=str(tmp_path), timeout=120,
                     shell=shell)
    assert bad.returncode != 0

    # Non-ASCII output decodes, and quoted arguments with spaces survive.
    script = tmp_path / "say.py"
    script.write_text("import sys; print('héllo ✓', sys.argv[1])", encoding="utf-8")
    out = shells.run(f'"{sys.executable}" "{script}" "two words"', cwd=str(tmp_path),
                     timeout=120, shell=shell)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "héllo ✓" in out.stdout and "two words" in out.stdout


@windows_only
def test_timeout_kills_the_whole_process_tree(tmp_path):
    """The shell's grandchildren must die too, or the pipes stay open."""
    import time

    child = tmp_path / "sleeper.py"
    child.write_text("import time; time.sleep(120)", encoding="utf-8")
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        shells.run(f'"{sys.executable}" "{child}"', cwd=str(tmp_path), timeout=3)
    assert time.monotonic() - start < 30  # returns promptly, not after 120s


@windows_only
def test_cmd_shim_receives_arguments_intact(tmp_path):
    """Outside agent CLIs are .cmd shims; cmd.exe re-parses their arguments."""
    from coded.external_agents import ExternalAgent

    script = tmp_path / "echo_arg.py"
    script.write_text("import sys; print('GOT:' + sys.argv[1])", encoding="utf-8")
    shim = tmp_path / "agent.cmd"
    shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")

    agent = ExternalAgent(name="shim", command=[str(shim), "{prompt}"], stdin=False)
    prompt = 'fix "quotes" & echo pwned'
    code, out = agent.run(prompt, str(tmp_path))
    assert "pwned" not in out.replace("GOT:" + prompt, "")  # the tail was not executed
    assert prompt in out


def test_config_shell_setting_applies_before_anything_uses_it(tmp_path, monkeypatch):
    """`"shell": …` must be in force before plugin hooks and the prompt resolve."""
    from coded.config import load_config

    monkeypatch.setattr(shells, "IS_WINDOWS", True)
    monkeypatch.setattr(shells, "resolve_shell",
                        lambda configured=None, **kw: Shell(name=str(configured), path="x.exe",
                                                            posix_syntax=False))
    (tmp_path / "config.json").write_text('{"shell": "pwsh"}')
    try:
        shells.set_active_shell(None)
        cfg = load_config(cwd=str(tmp_path), config_path=str(tmp_path / "config.json"))
        assert cfg.shell == "pwsh"
        assert shells.active_shell().name == "pwsh"
        # --shell pins: config.json must not overwrite it.
        shells.set_active_shell(Shell(name="cmd", path="cmd.exe", posix_syntax=False),
                                pinned=True)
        load_config(cwd=str(tmp_path), config_path=str(tmp_path / "config.json"))
        assert shells.active_shell().name == "cmd"
    finally:
        shells.set_active_shell(None)
