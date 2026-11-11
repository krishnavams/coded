"""Editable prompt files.

coded's prompts live in Markdown files that can be overridden one at a time:

    built-in   coded/builtin_prompts/<name>.md
    user       ~/.config/coded/prompts/<name>.md     (overrides built-in)
    project    ./.coded/prompts/<name>.md            (overrides user)

An override replaces the whole file. An empty override file removes that
section. Files are used verbatim (no templating), so braces and JSON are safe.

    system    identity, tone, output style          main agent
    coder     how to do the work                   main agent + sub-agents
    tools     tool-calling rules                   main agent + sub-agents
    security  safety rules                         main agent + sub-agents
    skills    intro to the skills list             when any skills exist
    planner   planning method                      planner skill, plan mode
    reviewer  review method                        reviewer skill (/review, coded review)
    debugger  debugging method                     debugger skill
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from coded.config import user_config_path

PROMPT_NAMES = ("system", "coder", "tools", "security", "skills", "planner", "reviewer", "debugger")


def builtin_prompts_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin_prompts"


def prompt_dirs(cwd: Optional[str] = None) -> List[Tuple[str, Path]]:
    """(label, dir) in increasing precedence."""
    dirs = [("built-in", builtin_prompts_dir()),
            ("user", user_config_path().parent / "prompts")]
    if cwd:
        dirs.append(("project", Path(cwd) / ".coded" / "prompts"))
    return dirs


def resolve_prompt(name: str, cwd: Optional[str] = None) -> Tuple[str, str]:
    """(text, source path) of the highest-precedence file for `name`."""
    text, source = "", ""
    for _label, d in prompt_dirs(cwd):
        p = d / f"{name}.md"
        if p.is_file():
            try:
                text, source = p.read_text(encoding="utf-8"), str(p)
            except OSError:
                continue
    return text.strip(), source


def load_prompt(name: str, cwd: Optional[str] = None) -> str:
    return resolve_prompt(name, cwd)[0]


def join_sections(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p and p.strip())


def prompts_main(argv: List[str]) -> int:
    """`coded prompts list|show|init`."""
    import argparse
    import os
    import shutil

    from coded import ui

    p = argparse.ArgumentParser(prog="coded prompts", description="Inspect and override prompt files.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Which file each prompt comes from.").add_argument("--cwd")
    sh = sub.add_parser("show", help="Print one prompt file, or the assembled system prompt.")
    sh.add_argument("name", nargs="?", help=f"One of: {', '.join(PROMPT_NAMES)}, or 'subagent'.")
    sh.add_argument("--cwd")
    it = sub.add_parser("init", help="Copy a built-in prompt to the user (or --project) dir to edit.")
    it.add_argument("names", nargs="+", help="Prompt names, or 'all'.")
    it.add_argument("--project", action="store_true")
    it.add_argument("--force", action="store_true", help="Overwrite an existing override.")
    it.add_argument("--cwd")
    args = p.parse_args(argv)
    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()

    if args.cmd == "list":
        for name in PROMPT_NAMES:
            text, source = resolve_prompt(name, cwd)
            label = next((lbl for lbl, d in prompt_dirs(cwd) if source.startswith(str(d))), "?")
            state = f"{len(text):>5} chars" if text else "  (removed: empty override)"
            print(f"{name:<10} {label:<9} {state}  {source}")
        return 0
    if args.cmd == "show":
        if args.name in (None, "system-prompt", "all"):
            from coded.prompts import build_system_prompt

            print(build_system_prompt(cwd))
        elif args.name == "subagent":
            from coded.prompts import build_subagent_prompt

            print(build_subagent_prompt(cwd))
        elif args.name in PROMPT_NAMES:
            print(load_prompt(args.name, cwd))
        else:
            ui.error(f"Unknown prompt '{args.name}'. Choose from: {', '.join(PROMPT_NAMES)}, subagent.")
            return 1
        return 0
    names = list(PROMPT_NAMES) if args.names == ["all"] else args.names
    bad = [n for n in names if n not in PROMPT_NAMES]
    if bad:
        ui.error(f"Unknown prompt(s): {', '.join(bad)}.")
        return 1
    dest_dir = prompt_dirs(cwd)[2 if args.project else 1][1]
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        dest = dest_dir / f"{name}.md"
        if dest.exists() and not args.force:
            ui.warn(f"{dest} exists; skipped (use --force to overwrite).")
            continue
        shutil.copyfile(builtin_prompts_dir() / f"{name}.md", dest)
        ui.success(f"Wrote {dest} — edit it; an empty file removes the section.")
    return 0
