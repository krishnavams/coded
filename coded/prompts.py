"""System prompt(s) for the agent."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import List

from coded.config import user_config_path
from coded.prompt_files import join_sections, load_prompt


# Instruction files, in precedence order within one directory. AGENTS.md is the
# tool-neutral convention (agents.md); CODED.md/CLAUDE.md are read too.
INSTRUCTION_FILES = ("AGENTS.md", "CODED.md", "CLAUDE.md", "AGENTS.override.md", "CODED.local.md")
_MAX_FILE_CHARS = 12_000
_MAX_TOTAL_CHARS = 32_000


def _project_root(cwd: Path) -> Path:
    """The enclosing git repo root, or cwd itself."""
    for d in (cwd, *cwd.parents):
        if (d / ".git").exists():
            return d
    return cwd


def _dir_instruction_files(directory: Path) -> List[Path]:
    """Instruction files in one directory, skipping byte-identical duplicates
    (e.g. a CLAUDE.md symlinked to AGENTS.md)."""
    out: List[Path] = []
    seen: set = set()
    for name in INSTRUCTION_FILES:
        p = directory / name
        if not p.is_file():
            continue
        try:
            key = p.read_bytes()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def instruction_files(cwd: str) -> List[Path]:
    """User-global files, then every directory from the repo root down to cwd —
    so more specific (deeper) instructions come last and take precedence."""
    files: List[Path] = []
    user_dir = user_config_path().parent
    files += _dir_instruction_files(user_dir)
    here = Path(cwd).resolve()
    root = _project_root(here)
    chain = [here] + [d for d in here.parents if d == root or root in d.parents]
    for d in reversed(chain):
        files += _dir_instruction_files(d)
    return files


def read_instructions(paths: List[Path], cwd: str, budget: int = _MAX_TOTAL_CHARS) -> str:
    parts = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")[:_MAX_FILE_CHARS]
        except OSError:
            continue
        if len(text) > budget:
            text = text[:budget]
        budget -= len(text)
        try:
            label = os.path.relpath(p, cwd)
        except ValueError:
            label = str(p)
        if label.startswith(".."):
            home = str(Path.home())
            label = "~" + str(p)[len(home):] if str(p).startswith(home) else str(p)
        parts.append(f"## {label}\n{text.strip()}\n")
        if budget <= 0:
            break
    return "\n".join(parts)


def _project_context(cwd: str) -> str:
    """Include AGENTS.md / CODED.md / CLAUDE.md files from ~/.config/coded and from
    the repo root down to cwd. Files in subdirectories are surfaced lazily, when
    the agent first reads a file there (see Agent._nested_instructions)."""
    text = read_instructions(instruction_files(cwd), cwd)
    if not text:
        return ""
    return ("\n\n# Project instructions\nFollow these. Deeper (more specific) files take "
            "precedence over earlier ones.\n\n" + text)


def _environment(cwd: str) -> str:
    return (f"# Environment\n- Working directory: {cwd}\n"
            f"- Platform: {platform.system()} ({platform.machine()})\n"
            f"- Today: {os.environ.get('CODED_DATE', '')}\n")


def build_system_prompt(cwd: str, extra: str | None = None) -> str:
    """system + coder + tools + security prompt files, environment, project
    instructions, then extras (skills list, --system). See prompt_files.py."""
    prompt = join_sections(*(load_prompt(n, cwd) for n in ("system", "coder", "tools", "security")),
                           _environment(cwd))
    prompt += _project_context(cwd)
    if extra:
        prompt += f"\n\n# Additional instructions\n{extra}\n"
    return prompt


_SUBAGENT_HEAD = """You are a focused sub-agent spawned by the coded coding agent to complete one \
specific, self-contained task. You have the same file and shell tools.

Do the task fully and autonomously, then return a concise final report of what you found \
or did. You cannot ask the user questions — make reasonable assumptions and state them."""


def build_subagent_prompt(cwd: str) -> str:
    """Sub-agents get the work, tool and safety rules but not the chat style."""
    return join_sections(_SUBAGENT_HEAD,
                         *(load_prompt(n, cwd) for n in ("coder", "tools", "security")),
                         _environment(cwd))
