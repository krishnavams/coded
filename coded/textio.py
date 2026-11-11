"""Reading and writing text files without changing their line endings.

Python's text mode translates line endings twice: reading turns ``\\r\\n`` into
``\\n``, and writing turns every ``\\n`` into the platform's ending. On Windows
that rewrites an entire LF file as CRLF the first time one line is edited, so a
one-line change lands as a whole-file diff.

coded therefore reads bytes, works on ``\\n`` text throughout (so the model's
``old_string`` matches), and writes the file back with the ending it already
had — CRLF stays CRLF, LF stays LF, on every platform.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple, Union

CRLF = "\r\n"
LF = "\n"


def detect_newline(data: Union[bytes, str], default: str = LF) -> str:
    """The file's dominant line ending ("\\r\\n" or "\\n")."""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    crlf = data.count(b"\r\n")
    if not crlf:
        return LF if b"\n" in data else default
    lf = data.count(b"\n") - crlf
    return CRLF if crlf >= lf else LF


def decode(data: bytes) -> Tuple[str, str]:
    """(text with \\n endings, the file's original ending).

    Decoding is strict: a file coded cannot decode must not be rewritten from a
    lossy reading of it, so callers report the error and leave it alone.
    """
    newline = detect_newline(data)
    text = data.decode("utf-8")  # raises UnicodeDecodeError
    return text.replace(CRLF, LF), newline


def encode(text: str, newline: str = LF) -> bytes:
    """Text (with \\n endings) as bytes using `newline`."""
    out = text.replace(CRLF, LF)
    if newline != LF:
        out = out.replace(LF, newline)
    return out.encode("utf-8")


NOT_UTF8 = ("is not valid UTF-8 text; coded will not rewrite it (that would "
            "corrupt the file). Convert it to UTF-8 first.")


def read_text(path: Union[str, Path]) -> Tuple[str, str]:
    """(contents with \\n endings, the file's ending).

    Raises OSError like open(), or UnicodeDecodeError for non-UTF-8 files.
    """
    return decode(Path(path).read_bytes())


def write_text(path: Union[str, Path], text: str, newline: str = LF) -> None:
    """Write `text` (with \\n endings) using `newline`, with no translation."""
    Path(path).write_bytes(encode(text, newline))


def newline_of(path: Union[str, Path], default: str = LF) -> str:
    """The ending an existing file uses, `default` if it doesn't exist."""
    try:
        return detect_newline(Path(path).read_bytes(), default)
    except OSError:
        return default
