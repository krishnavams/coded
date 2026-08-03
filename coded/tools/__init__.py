"""Built-in tool registry construction."""

from __future__ import annotations

from coded.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from coded.tools.files import EditTool, LsTool, ReadTool, WriteTool
from coded.tools.search import GlobTool, GrepTool
from coded.tools.shell import BashTool
from coded.tools.task import TaskTool

__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
]


def build_registry(include_task: bool = True) -> ToolRegistry:
    """Create a registry with the standard tool suite.

    Sub-agents pass ``include_task=False`` to avoid unbounded recursion.
    """
    reg = ToolRegistry()
    for tool in (ReadTool(), WriteTool(), EditTool(), LsTool(), GlobTool(), GrepTool(), BashTool()):
        reg.register(tool)
    if include_task:
        reg.register(TaskTool())
    return reg
