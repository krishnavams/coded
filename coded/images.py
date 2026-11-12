"""Helpers for attaching images to messages (multimodal / vision models)."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def encode_image_data_url(path: str) -> str:
    """Read an image file and return a base64 ``data:`` URL.

    Accepts a local path or an existing http(s)/data URL (returned unchanged),
    so a vision model can be handed either.
    """
    if path.startswith(("http://", "https://", "data:")):
        return path
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Image not found: {p}")
    mime = _EXT_MIME.get(p.suffix.lower()) or mimetypes.guess_type(str(p))[0] or "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------
_MAC_SCRIPT = [
    "try",
    "set d to (the clipboard as «class PNGf»)",
    'set f to open for access POSIX file "{path}" with write permission',
    "write d to f",
    "close access f",
    'return "ok"',
    "on error",
    'return "none"',
    "end try",
]
_WIN_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
    "$i = [System.Windows.Forms.Clipboard]::GetImage(); "
    "if ($i) {{ $i.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png); 'ok' }}"
)


def clipboard_commands(path: str, platform: str, which) -> list:
    """The commands that would save the clipboard's image to `path`, in order
    of preference, for this platform. Each is (argv, writes_stdout)."""
    cmds = []
    if platform == "darwin":
        if which("pngpaste"):
            cmds.append((["pngpaste", path], False))
        argv = ["osascript"]
        for line in _MAC_SCRIPT:
            argv += ["-e", line.replace("{path}", path)]
        cmds.append((argv, False))
    elif platform == "win32":
        shell = which("powershell") or which("pwsh")
        if shell:
            cmds.append(([shell, "-NoProfile", "-STA", "-Command",
                          _WIN_SCRIPT.format(path=path)], False))
    else:
        if which("wl-paste"):
            cmds.append((["wl-paste", "--no-newline", "--type", "image/png"], True))
        if which("xclip"):
            cmds.append((["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], True))
    return cmds


def grab_clipboard_image(dest_dir: str | None = None, *, platform: str | None = None,
                         which=None, run=None) -> str | None:
    """Save the image on the clipboard as a PNG and return its path, or None
    when the clipboard holds no image or no clipboard tool is installed.

    macOS: pngpaste or osascript; Windows: PowerShell; Linux: wl-paste
    (Wayland) or xclip (X11). `platform`/`which`/`run` are injectable for tests."""
    import shutil
    import subprocess
    import sys
    import tempfile
    import time

    platform = platform or sys.platform
    which = which or shutil.which
    run = run or subprocess.run
    folder = Path(dest_dir or Path(tempfile.gettempdir()) / "coded-clipboard")
    folder.mkdir(parents=True, exist_ok=True)
    path = str(folder / f"clipboard-{int(time.time() * 1000)}.png")
    for argv, to_stdout in clipboard_commands(path, platform, which):
        try:
            res = run(argv, capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001 - a missing or hung tool: try the next
            continue
        if getattr(res, "returncode", 1) != 0:
            continue
        if to_stdout:
            data = res.stdout or b""
            if not data.startswith(b"\x89PNG"):
                continue
            Path(path).write_bytes(data)
        p = Path(path)
        if p.is_file() and p.stat().st_size > 0:
            return path
    return None


def clipboard_hint(platform: str | None = None) -> str:
    """What to install when no clipboard tool is found."""
    import sys

    platform = platform or sys.platform
    if platform == "darwin":
        return "copy an image first (pngpaste makes it faster: brew install pngpaste)"
    if platform == "win32":
        return "copy an image first"
    return "copy an image first; reading it needs wl-clipboard (Wayland) or xclip (X11)"
