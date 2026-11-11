"""System prompt(s) for the agent."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any, List

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


def read_instructions(paths: List[Path], cwd: str, budget: int = _MAX_TOTAL_CHARS,
                      file_chars: int = _MAX_FILE_CHARS) -> str:
    parts = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")[:file_chars]
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


def _project_context(cwd: str, limits: Any = None) -> str:
    """Include AGENTS.md / CODED.md / CLAUDE.md files from ~/.config/coded and from
    the repo root down to cwd. Files in subdirectories are surfaced lazily, when
    the agent first reads a file there (see Agent._nested_instructions)."""
    if limits is not None:
        text = read_instructions(instruction_files(cwd), cwd, budget=limits.instructions_total_chars,
                                 file_chars=limits.instructions_file_chars)
    else:
        text = read_instructions(instruction_files(cwd), cwd)
    if not text:
        return ""
    return ("\n\n# Project instructions\nFollow these. Deeper (more specific) files take "
            "precedence over earlier ones.\n\n" + text)


def _environment(cwd: str, model: Any = None) -> str:
    from coded.shells import shell_summary

    label, posix = shell_summary()
    lines = [f"# Environment\n- Working directory: {cwd}",
             f"- Platform: {platform.system()} ({platform.machine()})",
             f"- Shell for the bash tool: {label}"]
    if not posix:
        lines.append("  Write commands in that shell's syntax, not POSIX sh: no `&&` chains "
                     "that assume sh, no `$(…)`, no single-quote escaping. Prefer the "
                     "read/write/edit/grep/glob tools over shell equivalents.")
    lines.append(f"- Today: {os.environ.get('CODED_DATE', '')}")
    if model is not None and getattr(model, "context_window", None):
        lines.append(f"- Model: {model.name} (context window ~{model.context_window // 1000}k tokens)")
    return "\n".join(lines) + "\n"


def build_system_prompt(cwd: str, extra: str | None = None, model: Any = None,
                        limits: Any = None) -> str:
    """system + coder + tools + security prompt files (for `model`'s prompt set),
    environment, project instructions, repository overview (large-context models),
    then extras (skills list, --system). See prompt_files.py."""
    prompt = join_sections(*(load_prompt(n, cwd, model)
                             for n in ("system", "coder", "tools", "security")),
                           _environment(cwd, model))
    prompt += _project_context(cwd, limits)
    if limits is not None and limits.repo_overview_chars > 0:
        from coded.repo_overview import build_repo_overview

        overview = build_repo_overview(cwd, limits.repo_overview_chars)
        if overview:
            prompt += "\n\n" + overview
    if extra:
        prompt += f"\n\n# Additional instructions\n{extra}\n"
    return prompt


_SUBAGENT_HEAD = """You are a focused sub-agent spawned by the coded coding agent to complete one \
specific, self-contained task. You have the same file and shell tools.

Do the task fully and autonomously, then return a concise final report of what you found \
or did. You cannot ask the user questions — make reasonable assumptions and state them. \
You have no task or exit_plan_mode tool: do the work yourself instead of delegating."""


def build_subagent_prompt(cwd: str, model: Any = None) -> str:
    """Sub-agents get the work, tool and safety rules but not the chat style, and
    no repository overview (they're given focused tasks)."""
    return join_sections(_SUBAGENT_HEAD,
                         *(load_prompt(n, cwd, model) for n in ("coder", "tools", "security")),
                         _environment(cwd, model))
