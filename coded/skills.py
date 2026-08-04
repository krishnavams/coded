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
from typing import Any, Dict, List, Tuple

from coded.config import user_config_path


def _scalar(v: str) -> Any:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _parse_yaml_block(lines: List[str]) -> Dict[str, Any]:
    """A deliberately small YAML subset: scalars, inline lists, block lists."""
    meta: Dict[str, Any] = {}
    key: str | None = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # Block-list item under the current key.
        if raw[:1] in (" ", "\t") and raw.lstrip().startswith("- ") and key is not None:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(_scalar(raw.lstrip()[2:]))
            continue
        if ":" in raw:
            k, _, v = raw.partition(":")
            key = k.strip()
            v = v.strip()
            if v == "":
                meta[key] = ""  # a block list may follow
            elif v.startswith("[") and v.endswith("]"):
                inner = v[1:-1]
                meta[key] = [_scalar(x) for x in inner.split(",") if x.strip()]
            else:
                meta[key] = _scalar(v)
    return meta


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

    def load_body(self) -> str:
        _, body = parse_frontmatter(self.path.read_text(encoding="utf-8"))
        return body


def builtin_skills_dir() -> Path:
    """Role skills bundled with the package (planner, reviewer, qa, security)."""
    return Path(__file__).resolve().parent / "builtin_skills"


def skill_search_dirs(cwd: str) -> List[Path]:
    # Order matters: later dirs override earlier ones by skill name, so a
    # user/project skill can shadow a built-in role of the same name.
    return [
        builtin_skills_dir(),                    # bundled roles
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
            )
    return skills


def skills_prompt_section(skills: Dict[str, Skill]) -> str:
    """Level-1 disclosure: names + descriptions injected into the system prompt."""
    if not skills:
        return ""
    out = [
        "# Available skills",
        "When the user's task matches one of these skills, call the `skill` tool "
        "with its name to load the full instructions, then follow them. Do not "
        "guess a skill's contents from its description alone.",
        "",
    ]
    for s in skills.values():
        out.append(f"- **{s.name}**: {s.description}")
    return "\n".join(out) + "\n"
