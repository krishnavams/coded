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
