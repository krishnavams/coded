"""The `skill` tool: level-2 progressive disclosure.

Calling it loads a skill's full SKILL.md body into the conversation as the tool
result, so the model then follows those instructions. Bundled files in the
skill's directory are reached with the ordinary read/bash tools (level 3).
"""

from __future__ import annotations

from typing import Any, Dict

from coded.tools.base import Tool, ToolContext, ToolResult


class SkillTool(Tool):
    name = "skill"
    description = """Invoke a named skill to load its full instructions into context, then \
follow them. Use this when the task matches a skill listed under "Available skills". The \
skill's bundled files (in its directory) can be read with `read` or run with `bash`."""
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill name to invoke."},
            "args": {"type": "string", "description": "Optional arguments/context for the skill."},
        },
        "required": ["name"],
    }

    def __init__(self, skills: Dict[str, Any]):
        self._skills = skills

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("name", "")
        skill = self._skills.get(name)
        if skill is None:
            available = ", ".join(self._skills) or "(none)"
            return ToolResult.error(f"Unknown skill '{name}'. Available skills: {available}")
        try:
            body = skill.load_body()
        except OSError as exc:
            return ToolResult.error(f"Could not read skill '{name}': {exc}")

        header = (
            f"Skill '{skill.name}' loaded from {skill.directory}. "
            "Follow these instructions for the current task. Files in that directory "
            "can be read with `read` or run with `bash`."
        )
        if skill.allowed_tools:
            header += f" Preferred tools: {', '.join(skill.allowed_tools)}."
        if args.get("args"):
            header += f"\nInvocation arguments: {args['args']}"
        return ToolResult.ok(f"{header}\n\n---\n{body}")
