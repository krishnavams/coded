"""Custom slash commands: reusable prompt templates stored as Markdown.

Locations (project overrides user on name clash):
  - user:    ~/.config/coded/commands/<name>.md
  - project: ./.coded/commands/<name>.md

Subdirectories namespace the command: ``commands/git/pr.md`` → ``/git:pr``.

Optional frontmatter::

    ---
    description: Open a PR for the current branch
    argument-hint: <title>
    model: smart            # switch to this model alias for the command
    ---
    Create a pull request titled "$ARGUMENTS" ...

Placeholders: ``$ARGUMENTS`` (the whole argument string) and ``$1`` … ``$9``
(whitespace-separated arguments). If the body has no placeholder, arguments
are appended after it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from coded.config import user_config_path
from coded.skills import parse_frontmatter


@dataclass
class CustomCommand:
    name: str
    description: str
    body: str
    path: str
    argument_hint: str = ""
    model: Optional[str] = None

    def expand(self, arguments: str = "") -> str:
        arguments = arguments.strip()
        parts = arguments.split()
        text = self.body
        has_placeholder = "$ARGUMENTS" in text or re.search(r"\$[1-9]", text)
        text = text.replace("$ARGUMENTS", arguments)
        text = re.sub(r"\$([1-9])",
                      lambda m: parts[int(m.group(1)) - 1] if int(m.group(1)) <= len(parts) else "",
                      text)
        if arguments and not has_placeholder:
            text = f"{text.rstrip()}\n\nArguments: {arguments}"
        return text


def command_dirs(cwd: str) -> list:
    return [user_config_path().parent / "commands", Path(cwd) / ".coded" / "commands"]


def discover_commands(cwd: str) -> Dict[str, CustomCommand]:
    found: Dict[str, CustomCommand] = {}
    for base in command_dirs(cwd):
        if not base.is_dir():
            continue
        for md in sorted(base.rglob("*.md")):
            rel = md.relative_to(base).with_suffix("")
            name = ":".join(rel.parts)
            try:
                meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
            except OSError:
                continue
            first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
            found[name] = CustomCommand(
                name=name,
                description=str(meta.get("description") or first_line[:80]),
                body=body,
                path=str(md),
                argument_hint=str(meta.get("argument-hint") or ""),
                model=meta.get("model") or None,
            )
    return found
