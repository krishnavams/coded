"""The agentic loop: call the model, run requested tools, repeat until done."""

from __future__ import annotations

import json
from typing import Any, Callable, List, Optional

from rich.live import Live
from rich.markdown import Markdown

from coded import ui
from coded.config import Config, ModelConfig
from coded.llm import Completion, LLMClient
from coded.permissions import Decision, PermissionManager
from coded.prompts import SUBAGENT_SYSTEM_PROMPT, build_system_prompt
from coded.session import Session
from coded.tools import ToolContext, ToolRegistry, build_registry


class Agent:
    def __init__(
        self,
        *,
        model: ModelConfig,
        config: Config,
        registry: ToolRegistry,
        session: Session,
        permissions: PermissionManager,
        cwd: str,
        stream: bool = True,
        verbose: bool = True,
        extra_tools: Optional[list] = None,
        checkpoints: Any = None,
    ):
        self.model = model
        self.config = config
        self.registry = registry
        self.session = session
        self.permissions = permissions
        self.cwd = cwd
        self.stream = stream
        self.verbose = verbose
        self.checkpoints = checkpoints
        self.llm = LLMClient(model)
        for t in extra_tools or []:
            self.registry.register(t)
        self.skills: dict = {}
        self._interrupted = False

    # -- public API ---------------------------------------------------------
    def run(self, user_message: Any, images: Optional[list] = None) -> str:
        """Run one user turn to completion. Returns the final assistant text.

        `images` is an optional list of image file paths to attach (for
        vision-capable models).
        """
        if self.checkpoints is not None:
            label = user_message if isinstance(user_message, str) else "turn"
            self.checkpoints.begin(str(label)[:60])
        if images:
            self.session.add_user_multimodal(str(user_message), images)
        else:
            self.session.add_user(user_message)
        return self._loop()

    def switch_model(self, model: ModelConfig) -> None:
        self.model = model
        self.llm = LLMClient(model)

    # -- core loop ----------------------------------------------------------
    def _loop(self) -> str:
        final_text = ""
        for _ in range(self.config.max_turns):
            self.session.turns += 1
            self._maybe_compact()
            completion = self._call_model()
            self.session.record_usage(completion.usage)
            if completion.usage.prompt_tokens:
                self.session.last_prompt_tokens = completion.usage.prompt_tokens

            if completion.content and self.verbose and not self.stream:
                ui.assistant_markdown(completion.content)

            self.session.add_assistant(completion.content, completion.tool_calls)

            if not completion.tool_calls:
                final_text = completion.content
                break

            # Execute each requested tool and feed results back.
            for tc in completion.tool_calls:
                result = self._run_tool(tc)
                self.session.add_tool_result(tc.id, result)
        else:
            if self.verbose:
                ui.warn(f"Reached max turns ({self.config.max_turns}); stopping.")
        return final_text

    def _maybe_compact(self, force: bool = False) -> bool:
        if not force and not self.config.auto_compact:
            return False
        from coded.compaction import maybe_compact

        did = maybe_compact(self.session, self.model, self.llm,
                            ratio=self.config.compact_ratio, force=force)
        if did and self.verbose:
            ui.info("(summarized earlier conversation to stay within the context window)")
        return did

    def _call_model(self) -> Completion:
        tools = self.registry.openai_schema() if self.model.supports_tools else None
        if self.stream and self.verbose:
            return self._call_streaming(tools)
        return self.llm.complete(self.session.messages, tools, stream=self.stream)

    def _call_streaming(self, tools) -> Completion:
        """Stream assistant text into a live-rendered Markdown block."""
        buffer: List[str] = []
        live = Live(console=ui.console, refresh_per_second=12, transient=False)
        started = False

        def on_text(chunk: str) -> None:
            nonlocal started
            if not started:
                live.start()
                started = True
            buffer.append(chunk)
            live.update(Markdown("".join(buffer)))

        try:
            completion = self.llm.complete(
                self.session.messages, tools, stream=True, on_text=on_text
            )
        finally:
            if started:
                live.update(Markdown("".join(buffer)))
                live.stop()
        return completion

    # -- tool execution -----------------------------------------------------
    def _run_tool(self, tc) -> str:
        tool = self.registry.get(tc.name)
        try:
            args = json.loads(tc.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            if self.verbose:
                ui.tool_call(tc.name, {"_raw": tc.arguments})
                ui.tool_result(f"Invalid arguments: {exc}", is_error=True)
            return f"Error: could not parse tool arguments as JSON: {exc}"

        if tool is None:
            if self.verbose:
                ui.tool_call(tc.name, args)
                ui.tool_result(f"Unknown tool '{tc.name}'.", is_error=True)
            return f"Error: unknown tool '{tc.name}'."

        if self.verbose:
            ui.tool_call(tc.name, args)

        preview_ctx = ToolContext(cwd=self.cwd, permissions=self.permissions,
                                  config=self.config, checkpoints=self.checkpoints)

        # Show a diff preview for mutating file tools before we prompt/apply.
        if self.verbose and tool.requires_permission:
            try:
                diff = tool.preview(args, preview_ctx)
            except Exception:  # noqa: BLE001 - preview must never break the loop
                diff = None
            if diff:
                ui.diff(diff)

        # Permission gate.
        if tool.requires_permission:
            decision = self.permissions.request(
                key=tc.name,
                title=self._perm_title(tc.name),
                detail=tool.permission_detail(args),
                target=tool.permission_target(args),
            )
            if decision is Decision.DENY:
                if self.verbose:
                    ui.tool_result("Denied.", is_error=True)
                return "The user denied permission to run this action. Do not retry it; ask how to proceed or try another approach."

        ctx = ToolContext(
            cwd=self.cwd,
            permissions=self.permissions,
            config=self.config,
            spawn_subagent=self._make_subagent_runner(),
            checkpoints=self.checkpoints,
        )
        try:
            result = tool.run(args, ctx)
        except Exception as exc:  # noqa: BLE001 - tools must never crash the loop
            if self.verbose:
                ui.tool_result(f"Tool raised: {exc}", is_error=True)
            return f"Error: tool '{tc.name}' raised an exception: {exc}"

        if self.verbose:
            ui.tool_result(result.content, is_error=result.is_error)
        return result.content

    @staticmethod
    def _perm_title(tool_name: str) -> str:
        return {
            "bash": "Run shell command",
            "write": "Write file",
            "edit": "Edit file",
        }.get(tool_name, f"Run {tool_name}")

    # -- sub-agents ---------------------------------------------------------
    def run_subagent(self, prompt: str) -> str:
        """Run a one-shot sub-agent (isolated context) and return its report."""
        return self._make_subagent_runner()(prompt)

    def _make_subagent_runner(self) -> Callable[[str], str]:
        def runner(prompt: str) -> str:
            if self.verbose:
                ui.info("  ↳ spawning sub-agent…")
            sub_system = SUBAGENT_SYSTEM_PROMPT.format(cwd=self.cwd)
            sub_session = Session(system_prompt=sub_system)
            sub_registry = build_registry(include_task=False)
            sub_agent = Agent(
                model=self.model,
                config=self.config,
                registry=sub_registry,
                session=sub_session,
                permissions=self.permissions,
                cwd=self.cwd,
                stream=False,
                verbose=False,
                checkpoints=self.checkpoints,
            )
            result = sub_agent.run(prompt)
            # Roll the sub-agent's token usage up into the parent session.
            self.session.record_usage(sub_session.total_usage)
            return result

        return runner


def create_agent(
    *,
    model: ModelConfig,
    config: Config,
    cwd: str,
    permissions: PermissionManager,
    system_extra: Optional[str] = None,
    extra_tools: Optional[list] = None,
    skills: Optional[dict] = None,
    stream: bool = True,
    verbose: bool = True,
    checkpoints: Any = None,
) -> Agent:
    """Convenience constructor wiring together a fresh top-level agent."""
    from coded.checkpoints import CheckpointManager
    from coded.skills import skills_prompt_section
    from coded.tools.skill import SkillTool

    skills = skills or {}
    section = skills_prompt_section(skills)
    combined = "\n\n".join(x for x in (system_extra, section) if x) or None

    system_prompt = build_system_prompt(cwd, extra=combined)
    session = Session(system_prompt=system_prompt)
    registry = build_registry(include_task=True)
    if skills:
        registry.register(SkillTool(skills))

    if checkpoints is None:
        checkpoints = CheckpointManager(cwd)

    agent = Agent(
        model=model,
        config=config,
        registry=registry,
        session=session,
        permissions=permissions,
        cwd=cwd,
        stream=stream,
        verbose=verbose,
        extra_tools=extra_tools,
        checkpoints=checkpoints,
    )
    agent.skills = skills
    return agent
