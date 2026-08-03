"""OpenAI-compatible LLM client with streaming and tool-calling support."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openai import OpenAI

from coded.config import ModelConfig


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as produced by the model


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )


@dataclass
class Completion:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: Optional[str] = None


class LLMClient:
    """Thin wrapper over the OpenAI SDK pointed at any compatible endpoint."""

    def __init__(self, model: ModelConfig):
        self.model = model
        self.client = OpenAI(
            base_url=model.resolved_base_url(),
            api_key=model.resolved_api_key(),
            default_headers=model.extra_headers or None,
        )

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = True,
        on_text: Optional[Callable[[str], None]] = None,
    ) -> Completion:
        params: Dict[str, Any] = {
            "model": self.model.model,
            "messages": messages,
        }
        if self.model.max_tokens:
            params["max_tokens"] = self.model.max_tokens
        if self.model.temperature is not None:
            params["temperature"] = self.model.temperature
        if tools and self.model.supports_tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        if stream:
            return self._stream(params, on_text)
        return self._once(params)

    # -- non-streaming ------------------------------------------------------
    def _once(self, params: Dict[str, Any]) -> Completion:
        resp = self.client.chat.completions.create(**params)
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = []
        for tc in msg.tool_calls or []:
            tool_calls.append(ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}"))
        usage = Usage()
        if resp.usage:
            usage = Usage(resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
        return Completion(msg.content or "", tool_calls, usage, choice.finish_reason)

    # -- streaming ----------------------------------------------------------
    def _stream(
        self, params: Dict[str, Any], on_text: Optional[Callable[[str], None]]
    ) -> Completion:
        params = dict(params, stream=True)
        # Ask for usage in the final chunk where supported; ignore if unsupported.
        params["stream_options"] = {"include_usage": True}
        try:
            stream = self.client.chat.completions.create(**params)
        except Exception:
            params.pop("stream_options", None)
            stream = self.client.chat.completions.create(**params)

        content_parts: List[str] = []
        # index -> partial tool call
        partial: Dict[int, Dict[str, str]] = {}
        usage = Usage()
        finish_reason: Optional[str] = None

        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = Usage(chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if delta and delta.content:
                content_parts.append(delta.content)
                if on_text:
                    on_text(delta.content)
            for tc in (delta.tool_calls or []) if delta else []:
                idx = tc.index or 0
                slot = partial.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"] += tc.function.arguments

        tool_calls = [
            ToolCall(v["id"] or f"call_{i}", v["name"], v["arguments"] or "{}")
            for i, v in sorted(partial.items())
            if v["name"]
        ]
        return Completion("".join(content_parts), tool_calls, usage, finish_reason)
