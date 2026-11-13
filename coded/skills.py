"""Agent Skills — Claude Code-style, with progressive disclosure.

A skill is a directory containing a ``SKILL.md`` file with YAML frontmatter:

    ---
    name: my-skill
    description: What it does, and WHEN to use it (this drives auto-invocation).
    allowed-tools: read, edit, bash        # optional, informational
    ---
    # Full instructions the agent follows once the skill is invoked …

Discovery locations (project overrides user on name clash):
  - user:    ~/.config/coded/skills/<name>/SKILL.md
  - project: ./.coded/skills/<name>/SKILL.md

Progressive disclosure:
  L1  only name + description go into the system prompt (see skills_prompt_section)
  L2  the `skill` tool loads the full SKILL.md body into the conversation
  L3  the body references bundled files/scripts, read or run with read/bash
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from coded.config import user_config_path

_PROMPT_REF = re.compile(r"<!--\s*coded:prompt\s+([a-z0-9_-]+)\s*-->")


def _scalar(v: str) -> Any:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _parse_yaml_block(lines: List[str]) -> Dict[str, Any]:
    """A deliberately small YAML subset, enough for real SKILL.md files.

    Top-level keys start at column 0; indented lines belong to the key above:
    a block list (``- item``), a one-level mapping (``metadata:`` then
    ``  author: x``), a block scalar (``description: >`` / ``|``) or a plain
    scalar continued over several lines. Values may be inline lists.
    """
    entries: List[Tuple[str, str, List[str]]] = []  # (key, inline value, indented lines)
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            if entries and raw.strip() == "":
                entries[-1][2].append("")  # keeps paragraph breaks in `|` scalars
            continue
        if raw[:1] not in (" ", "\t") and ":" in raw:
            k, _, v = raw.partition(":")
            entries.append((k.strip(), v.strip(), []))
        elif entries:
            entries[-1][2].append(raw)
    meta: Dict[str, Any] = {}
    for key, value, more in entries:
        meta[key] = _yaml_value(value, more)
    return meta


def _yaml_value(value: str, more: List[str]) -> Any:
    body = [m for m in more]
    while body and body[-1] == "":
        body.pop()
    stripped = [m.strip() for m in body]
    if value[:1] in ("|", ">"):
        if value[0] == "|":
            return "\n".join(stripped).strip()
        return " ".join(x for x in stripped if x).strip()
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(x) for x in value[1:-1].split(",") if x.strip()]
    if value == "" and stripped and all(x.startswith("- ") or x == "-" or not x
                                        for x in stripped):
        return [_scalar(x[2:]) for x in stripped if x.startswith("- ")]
    if value == "" and stripped and re.match(r"^[\w.-]+:(\s|$)", stripped[0]):
        mapping: Dict[str, Any] = {}
        for x in stripped:
            if ":" in x:
                k, _, v = x.partition(":")
                mapping[k.strip()] = _scalar(v.strip()) if v.strip() else ""
        return mapping
    parts = [value] + [x for x in stripped if x]
    text = " ".join(p for p in parts if p).strip()
    return _scalar(text) if text else ""


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Split a document into (frontmatter dict, body). No frontmatter → ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta = _parse_yaml_block(lines[1:end])
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def _as_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [p.strip() for p in str(value).split(",") if p.strip()]


@dataclass
class Skill:
    name: str
    description: str
    path: Path  # the SKILL.md file
    directory: Path
    allowed_tools: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    cwd: Optional[str] = None  # for resolving prompt-file overrides

    def load_body(self, model: Any = None) -> str:
        """The skill's instructions. A body that is just ``<!-- coded:prompt NAME -->``
        is read from the prompt file NAME (so overriding e.g. reviewer.md changes
        the reviewer skill too)."""
        _, body = parse_frontmatter(self.path.read_text(encoding="utf-8"))
        m = _PROMPT_REF.fullmatch(body.strip())
        if m:
            from coded.prompt_files import load_prompt

            return load_prompt(m.group(1), self.cwd, model)
        return body


def builtin_skills_dir() -> Path:
    """Role skills bundled with the package (planner, reviewer, qa, security)."""
    return Path(__file__).resolve().parent / "builtin_skills"


def skill_search_dirs(cwd: str) -> List[Path]:
    # Order matters: later dirs override earlier ones by skill name:
    # built-in < enabled plugins < user < project.
    from coded.plugins import plugin_skill_dirs

    try:
        from_plugins = plugin_skill_dirs(cwd)
    except Exception:  # noqa: BLE001 - a broken plugin must not stop skill discovery
        from_plugins = []
    return [
        builtin_skills_dir(),                    # bundled roles
        *from_plugins,                           # <plugin>/skills
        user_config_path().parent / "skills",    # ~/.config/coded/skills
        Path(cwd) / ".coded" / "skills",         # project-local
    ]


def discover_skills(cwd: str) -> Dict[str, Skill]:
    """Find all skills. User/project skills override built-ins of the same name."""
    skills: Dict[str, Skill] = {}
    for base in skill_search_dirs(cwd):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            skill_md = child / "SKILL.md"
            if not (child.is_dir() and skill_md.is_file()):
                continue
            try:
                meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = str(meta.get("name") or child.name)
            skills[name] = Skill(
                name=name,
                description=str(meta.get("description", "")).strip(),
                path=skill_md,
                directory=child,
                allowed_tools=_as_list(meta.get("allowed-tools") or meta.get("allowed_tools")),
                metadata=meta,
                cwd=cwd,
            )
    return skills


def skills_prompt_section(skills: Dict[str, Skill], cwd: Optional[str] = None,
                          model: Any = None, budget: int = 0) -> str:
    """Level-1 disclosure: the skills.md intro, then names + descriptions.

    `budget` (characters, 0 = none) keeps a large collection from crowding a
    small model's window: past it, descriptions are cut to about 200
    characters, and what still does not fit is listed by name only — every
    skill stays loadable through the skill tool either way.
    """
    if not skills:
        return ""
    from coded.prompt_files import load_prompt

    intro = load_prompt("skills", cwd, model)
    head = [intro, ""] if intro else ["# Available skills", ""]
    full = [f"- **{s.name}**: {s.description}" for s in skills.values()]
    text = "\n".join(head + full) + "\n"
    if not budget or len(text) <= budget:
        return text
    used = len("\n".join(head)) + 1
    lines: List[str] = []
    rest: List[str] = []
    for s in skills.values():
        line = f"- **{s.name}**: {_shorten(s.description)}"
        if not rest and used + len(line) + 1 <= budget * 0.9:
            lines.append(line)
            used += len(line) + 1
        else:
            rest.append(s.name)
    if rest:
        lines.append(f"- Also installed ({len(rest)}, load by name with the skill tool when "
                     f"one fits the task): {', '.join(rest)}")
    return "\n".join(head + lines) + "\n"


def _shorten(text: str, limit: int = 200) -> str:
    """Cut on a word boundary; the "Use when…" clause often comes second."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def skill_message(name: str, body: str, task: str = "") -> str:
    """The user message that applies a skill now (`/skill <name>` or `/<name>`)."""
    task = task.strip()
    return (f"Apply the skill '{name}'. Its instructions are below; follow them"
            + (f" for this task: {task}\n\n" if task else ".\n\n")
            + f"---\n{body}")
