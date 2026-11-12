"""Built-in tool registry construction."""

from __future__ import annotations

from coded.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from coded.tools.files import DeleteTool, EditTool, LsTool, MoveTool, ReadTool, WriteTool
from coded.tools.git import GitTool
from coded.tools.interact import AskUserTool, ViewImageTool
from coded.tools.github import GitHubTool
from coded.tools.memory import MemoryTool
from coded.tools.notebook import NotebookEditTool
from coded.tools.patch import ApplyPatchTool, MultiEditTool
from coded.tools.repo_map import RepoMapTool
from coded.tools.search import GlobTool, GrepTool
from coded.tools.semantic_search import SemanticSearchTool
from coded.tools.shell import BashOutputTool, BashTool, KillShellTool
from coded.tools.task import TaskOutputTool, TaskStopTool, TaskTool
from coded.tools.todo import EnterPlanModeTool, ExitPlanModeTool, TodoWriteTool
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
        ReadTool(), WriteTool(), EditTool(), MultiEditTool(), ApplyPatchTool(),
        NotebookEditTool(),
        LsTool(), MoveTool(), DeleteTool(),
        GlobTool(), GrepTool(), SemanticSearchTool(), RepoMapTool(),
        BashTool(), BashOutputTool(), KillShellTool(),
        GitTool(), GitHubTool(), WebSearchTool(), WebFetchTool(),
        TodoWriteTool(), ViewImageTool(),
    ]
    for tool in tools:
        reg.register(tool)
    if include_task:
        reg.register(MemoryTool())  # main agent only: a sub-agent has no session to recall
        reg.register(TaskTool())
        reg.register(TaskOutputTool())
        reg.register(TaskStopTool())
        reg.register(ExitPlanModeTool())
        reg.register(EnterPlanModeTool())
        reg.register(AskUserTool())  # main agent only: a sub-agent reports back instead
    return reg
