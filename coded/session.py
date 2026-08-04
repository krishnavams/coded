"""Conversation state and running cost/usage tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from coded.config import ModelConfig
from coded.llm import Usage


@dataclass
class Session:
    system_prompt: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    total_usage: Usage = field(default_factory=Usage)
    turns: int = 0

    def __post_init__(self) -> None:
        if not self.messages:
            self.messages = [{"role": "system", "content": self.system_prompt}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_user_multimodal(self, text: str, image_paths: list) -> None:
        """Add a user message with attached images (for vision-capable models)."""
        from coded.images import encode_image_data_url

        parts: list = [{"type": "text", "text": text}]
        for path in image_paths:
            url = encode_image_data_url(path)
            parts.append({"type": "image_url", "image_url": {"url": url}})
        self.messages.append({"role": "user", "content": parts})

    def add_assistant(self, content: str, tool_calls: list | None = None) -> None:
        msg: Dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in tool_calls
            ]
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )

    def record_usage(self, usage: Usage) -> None:
        self.total_usage = self.total_usage + usage

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.total_usage = Usage()
        self.turns = 0

    def estimated_cost(self, model: ModelConfig) -> float:
        return (
            self.total_usage.prompt_tokens / 1_000_000 * model.input_cost
            + self.total_usage.completion_tokens / 1_000_000 * model.output_cost
        )
