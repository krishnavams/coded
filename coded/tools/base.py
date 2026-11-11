"""Tool framework: base class, result type, execution context, and registry."""

from __future__ import annotations

import copy
import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional


@dataclass
class ToolResult:
    content: str
    is_error: bool = False

    @classmethod
    def ok(cls, content: str) -> "ToolResult":
        return cls(content, False)

    @classmethod
    def error(cls, content: str) -> "ToolResult":
        return cls(content, True)


@dataclass
class ToolContext:
    """Everything a tool needs to run, injected by the agent."""

    cwd: str
    permissions: Any  # PermissionManager
    config: Any  # Config
    # Runs a sub-agent with `prompt` and returns its final text. Injected by Agent.
    spawn_subagent: Optional[Callable[[str], str]] = None
    # Records pre-change file state for /undo. Injected by Agent. May be None.
    checkpoints: Any = None  # CheckpointManager
    # Shared agent state: "todos" (TodoStore), "shells" (BackgroundShells),
    # "set_mode"/"get_mode" (plan mode), "external_agents", ...
    extra: Dict[str, Any] = field(default_factory=dict)


class Tool:
    """Base class for all tools."""

    name: str = ""
    description: str = ""
    # A shorter description for the compact prompt profile (small/local
    # models), where every token of every tool definition is paid each step.
    # Keep the rules that prevent mistakes; drop the explanation.
    brief: str = ""
    # JSON Schema for the tool's arguments.
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}
    requires_permission: bool = False
    # True for a tool that changes things outside the conversation but gates
    # itself per call (git). It makes the turn eligible for verification; a
    # tool with requires_permission already is.
    mutates: bool = False
    # Read-only tools that may run concurrently with each other.
    parallel_safe: bool = False
    # Output comes from outside the user's control (web pages, PR bodies, MCP).
    untrusted_output: bool = False

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------
    def permission_detail(self, args: Dict[str, Any]) -> str:
        """Human-readable description of what this call will do (for prompts)."""
        return f"{self.name}({args})"

    def permission_target(self, args: Dict[str, Any]) -> str:
        """The string matched against allow/deny rules (e.g. command or path)."""
        return ""

    def is_parallel_safe(self, args: Dict[str, Any], ctx: "ToolContext") -> bool:
        return self.parallel_safe

    def auto_allowed(self, args: Dict[str, Any], ctx: "ToolContext") -> bool:
        """True when this call may skip the prompt (e.g. it runs sandboxed)."""
        return False

    def preview(self, args: Dict[str, Any], ctx: "ToolContext"):
        """Optional colored/plain preview (e.g. a diff) shown before running."""
        return None

    def to_openai(self, compact: bool = False) -> Dict[str, Any]:
        description = (self.brief or self.description) if compact else self.description
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description.strip(),
                "parameters": _compact_parameters(self.parameters) if compact else self.parameters,
            },
        }


_PARAM_DESC_MAX = 120


def _compact_parameters(schema: Any) -> Any:
    """Long argument descriptions cut to their first sentence (compact profile)."""
    if not isinstance(schema, dict):
        return schema
    out = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            desc = node.get("description")
            if isinstance(desc, str) and len(desc) > _PARAM_DESC_MAX:
                m = re.search(r"(?<=[.!?])\s", desc)
                cut = desc[: m.start()] if m and m.start() <= _PARAM_DESC_MAX else desc
                if len(cut) > _PARAM_DESC_MAX:
                    cut = cut[: _PARAM_DESC_MAX - 1].rsplit(" ", 1)[0] + "…"
                node["description"] = cut
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(out)
    return out


def tool_disabled(name: str, patterns: Iterable[str]) -> bool:
    """Does `name` match any disabled-tool pattern ("github", "mcp__slack__*")?"""
    return any(fnmatch.fnmatchcase(name, p) for p in patterns or ())


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def openai_schema(self, compact: bool = False,
                      exclude: Iterable[str] = ()) -> List[Dict[str, Any]]:
        exclude = list(exclude or ())
        return [t.to_openai(compact) for t in self._tools.values()
                if not tool_disabled(t.name, exclude)]

    def names(self) -> List[str]:
        return list(self._tools)
