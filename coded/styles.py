"""Output styles: how the agent talks, switchable with `/style`.

A style is a Markdown file whose body is added to the system prompt; its
frontmatter `description` is shown in `/style`. Built-in styles live in
`coded/builtin_styles/`; your own go in `~/.config/coded/styles/<name>.md` or
`./.coded/styles/<name>.md` (a later location wins by name). Pick one with
`/style <name>`, `"output_style": "<name>"` in config, or `/style off`.

With no style set the system prompt is byte-for-byte what it was before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


@dataclass
class Style:
    name: str
    description: str
    body: str
    source: str


def style_dirs(cwd: str):
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return [Path(__file__).parent / "builtin_styles", Path(base) / "coded" / "styles",
            Path(cwd) / ".coded" / "styles"]


def discover_styles(cwd: str) -> Dict[str, Style]:
    from coded.skills import parse_frontmatter

    found: Dict[str, Style] = {}
    for folder in style_dirs(cwd):
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.md")):
            try:
                meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            body = body.strip()
            if body:
                found[f.stem] = Style(f.stem, str(meta.get("description", "")).strip(), body,
                                      str(f))
    return found


def style_section(name: Optional[str], cwd: str) -> str:
    """The system-prompt section for a style ("" for none or unknown)."""
    if not name or name in ("default", "off", "none"):
        return ""
    style = discover_styles(cwd).get(name)
    if style is None:
        return ""
    return f"# Output style: {style.name}\n\n{style.body}"
