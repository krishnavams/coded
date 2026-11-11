"""Editable prompt files.

coded's prompts live in Markdown files that can be overridden one at a time:

    built-in   coded/builtin_prompts/<name>.md
    user       ~/.config/coded/prompts/<name>.md     (overrides built-in)
    project    ./.coded/prompts/<name>.md            (overrides user)

    model      the model's "prompts_dir"                (overrides all; relative = project dir)

Each model also has a prompt set ("prompt_profile"): standard, compact (terse,
for small/local models) or full (more thorough, for large models). The compact
and full variants live in builtin_prompts/<profile>/ and replace only the files
they contain; "auto" picks by context window (<= 32k compact, >= 200k full).

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

import os
from pathlib import Path
from typing import Any, List, Optional, Tuple

from coded.config import user_config_path

PROMPT_NAMES = ("system", "coder", "tools", "security", "skills", "planner", "reviewer", "debugger")


PROFILES = ("standard", "compact", "full")


def builtin_prompts_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin_prompts"


def prompt_profile(model: Any = None) -> str:
    """A model's prompt set. "auto" picks compact for <= 32k context, full for
    >= 200k, standard otherwise. Context size isn't model size, so set
    prompt_profile explicitly when you know better."""
    if model is None:
        return "standard"
    profile = str(getattr(model, "prompt_profile", "auto") or "auto").lower()
    if profile in PROFILES:
        return profile
    window = getattr(model, "context_window", 128_000) or 128_000
    if window <= 32_000:
        return "compact"
    if window >= 200_000:
        return "full"
    return "standard"


def model_prompts_dir(model: Any, cwd: Optional[str]) -> Optional[Path]:
    raw = getattr(model, "prompts_dir", None) if model is not None else None
    if not raw:
        return None
    path = Path(os.path.expanduser(str(raw)))
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path  # relative to the project directory
    return path


def prompt_dirs(cwd: Optional[str] = None, model: Any = None) -> List[Tuple[str, Path]]:
    """(label, dir) in increasing precedence: built-in standard, built-in profile
    variant, user overrides, project overrides, the model's own prompts_dir."""
    dirs = [("built-in", builtin_prompts_dir())]
    profile = prompt_profile(model)
    if profile != "standard":
        dirs.append((f"built-in:{profile}", builtin_prompts_dir() / profile))
    dirs.append(("user", user_config_path().parent / "prompts"))
    if cwd:
        dirs.append(("project", Path(cwd) / ".coded" / "prompts"))
    mdir = model_prompts_dir(model, cwd)
    if mdir is not None:
        dirs.append((f"model:{getattr(model, 'name', '')}", mdir))
    return dirs


def resolve_prompt(name: str, cwd: Optional[str] = None, model: Any = None) -> Tuple[str, str]:
    """(text, source path) of the highest-precedence file for `name`."""
    text, source = "", ""
    for _label, d in prompt_dirs(cwd, model):
        p = d / f"{name}.md"
        if p.is_file():
            try:
                text, source = p.read_text(encoding="utf-8"), str(p)
            except OSError:
                continue
    return text.strip(), source


def load_prompt(name: str, cwd: Optional[str] = None, model: Any = None) -> str:
    return resolve_prompt(name, cwd, model)[0]


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
    ls = sub.add_parser("list", help="Which file each prompt comes from.")
    ls.add_argument("--cwd")
    ls.add_argument("-m", "--model", help="Resolve for this model alias (prompt set, prompts_dir).")
    sh = sub.add_parser("show", help="Print one prompt file, or the assembled system prompt.")
    sh.add_argument("name", nargs="?", help=f"One of: {', '.join(PROMPT_NAMES)}, or 'subagent'.")
    sh.add_argument("--cwd")
    sh.add_argument("-m", "--model", help="Assemble for this model alias.")
    it = sub.add_parser("init", help="Copy a built-in prompt to the user (or --project) dir to edit.")
    it.add_argument("names", nargs="+", help="Prompt names, or 'all'.")
    it.add_argument("--project", action="store_true")
    it.add_argument("--force", action="store_true", help="Overwrite an existing override.")
    it.add_argument("--cwd")
    args = p.parse_args(argv)
    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    model = None
    if getattr(args, "model", None):
        from coded.config import ConfigError, load_config

        try:
            model = load_config(cwd=cwd).get_model(args.model)
        except ConfigError as exc:
            ui.error(str(exc))
            return 1

    if args.cmd == "list":
        if model is not None:
            print(f"model {model.name}: prompt set '{prompt_profile(model)}'"
                  + (f", prompts_dir {model_prompts_dir(model, cwd)}" if model.prompts_dir else ""))
        for name in PROMPT_NAMES:
            text, source = resolve_prompt(name, cwd, model)
            matches = [(len(str(d)), lbl) for lbl, d in prompt_dirs(cwd, model)
                       if source == str(d / f"{name}.md")]
            label = max(matches)[1] if matches else "?"
            state = f"{len(text):>5} chars" if text else "  (removed: empty override)"
            print(f"{name:<10} {label:<16} {state}  {source}")
        return 0
    if args.cmd == "show":
        if args.name in (None, "system-prompt", "all"):
            from coded.limits import limits_for
            from coded.prompts import build_system_prompt
            from coded.skills import discover_skills, skills_prompt_section

            # Same assembly as create_agent (minus --system extras).
            budget = limits_for(model).skills_chars if model is not None else 0
            extra = skills_prompt_section(discover_skills(cwd), cwd, model, budget=budget) or None
            print(build_system_prompt(cwd, extra=extra, model=model,
                                      limits=limits_for(model) if model is not None else None))
        elif args.name == "subagent":
            from coded.prompts import build_subagent_prompt

            print(build_subagent_prompt(cwd, model))
        elif args.name in PROMPT_NAMES:
            print(load_prompt(args.name, cwd, model))
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
