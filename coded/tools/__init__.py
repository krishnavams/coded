"""Built-in tool registry construction."""

from __future__ import annotations

from coded.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from coded.tools.files import DeleteTool, EditTool, LsTool, MoveTool, ReadTool, WriteTool
from coded.tools.git import GitTool
from coded.tools.github import GitHubTool
from coded.tools.search import GlobTool, GrepTool
from coded.tools.semantic_search import SemanticSearchTool
from coded.tools.shell import BashTool
from coded.tools.task import TaskTool
from coded.tools.web import WebFetchTool, WebSearchTool

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
    tools = [
        ReadTool(), WriteTool(), EditTool(), LsTool(), MoveTool(), DeleteTool(),
        GlobTool(), GrepTool(), SemanticSearchTool(), BashTool(),
        GitTool(), GitHubTool(), WebSearchTool(), WebFetchTool(),
    ]
    for tool in tools:
        reg.register(tool)
    if include_task:
        reg.register(TaskTool())
    return reg
