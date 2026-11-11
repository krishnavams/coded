"""external_agent tool: delegate a task to an outside coding-agent CLI."""

from __future__ import annotations

import subprocess
from typing import Any, Dict

from coded.external_agents import ExternalAgent, describe
from coded.tools.base import Tool, ToolContext, ToolResult


class ExternalAgentTool(Tool):
    name = "external_agent"
    requires_permission = True

    def __init__(self, agents: Dict[str, ExternalAgent]):
        self.agents = agents
        self.description = (
            "Delegate a self-contained task to an outside coding agent and get back its "
            "final answer. Useful for a second opinion, a different model's strengths, or "
            "parallel work. The agent runs in the working directory with its own tools and "
            "permissions; give it a complete, standalone instruction. Use "
            "isolation=\"worktree\" when it may edit files, so its changes land on a "
            f"separate branch. Available: {describe(agents)}."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": sorted(agents)},
                "prompt": {"type": "string", "description": "Complete, standalone instruction."},
                "isolation": {"type": "string", "enum": ["none", "worktree"]},
            },
            "required": ["agent", "prompt"],
        }

    def permission_detail(self, args: Dict[str, Any]) -> str:
        agent = self.agents.get(args.get("agent", ""))
        cmd = " ".join(agent.command) if agent else args.get("agent", "?")
        prompt = str(args.get("prompt", ""))
        short = prompt if len(prompt) < 400 else prompt[:400] + "…"
        iso = "  [isolated worktree]" if args.get("isolation") == "worktree" else ""
        return f"{cmd}{iso}\n\n{short}"

    def permission_target(self, args: Dict[str, Any]) -> str:
        return args.get("agent", "")

    def is_parallel_safe(self, args: Dict[str, Any], ctx: ToolContext) -> bool:
        return args.get("isolation") == "worktree" and ctx.permissions.can_prompt_concurrently()

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        agent = self.agents.get(args.get("agent", ""))
        if agent is None:
            return ToolResult.error(
                f"Unknown agent '{args.get('agent')}'. Available: {', '.join(self.agents) or 'none'}.")
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult.error("Provide a prompt for the agent.")
        cwd, wt = ctx.cwd, None
        if args.get("isolation") == "worktree":
            from coded.worktree import WorktreeError, create_worktree

            try:
                wt = create_worktree(ctx.cwd, agent.name)
            except WorktreeError as exc:
                return ToolResult.error(f"Could not create worktree: {exc}")
            cwd = wt[0]
        try:
            code, out = agent.run(prompt, cwd)
        except FileNotFoundError:
            return ToolResult.error(f"'{agent.command[0]}' is not installed or not on PATH.")
        except subprocess.TimeoutExpired:
            return ToolResult.error(f"{agent.name} timed out after {agent.timeout}s.")
        except OSError as exc:
            return ToolResult.error(f"Could not run {agent.name}: {exc}")
        finally:
            where = ""
            if wt is not None:
                from coded.worktree import finalize_worktree

                try:
                    where = finalize_worktree(*wt)
                except Exception as exc:  # noqa: BLE001
                    where = f"(could not inspect worktree {wt[0]}: {exc})"
        body = out or "(no output)"
        if where:
            body += f"\n\n[worktree] {where}"
        return ToolResult(f"[{agent.name} exit {code}]\n{body}", is_error=code != 0)
