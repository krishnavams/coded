"""Tool framework: base class, result type, execution context, and registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


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
    # Names of models available (for the task tool description, etc.).
    extra: Dict[str, Any] = field(default_factory=dict)


class Tool:
    """Base class for all tools."""

    name: str = ""
    description: str = ""
    # JSON Schema for the tool's arguments.
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}
    requires_permission: bool = False

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------
    def permission_detail(self, args: Dict[str, Any]) -> str:
        """Human-readable description of what this call will do (for prompts)."""
        return f"{self.name}({args})"

    def to_openai(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def openai_schema(self) -> List[Dict[str, Any]]:
        return [t.to_openai() for t in self._tools.values()]

    def names(self) -> List[str]:
        return list(self._tools)
