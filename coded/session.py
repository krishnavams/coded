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
    # Input tokens of the most recent model call (drives compaction).
    last_prompt_tokens: int = 0
    compactions: int = 0
    # Cost accrued per answering model (routing and pools change it mid-session).
    cost: float = 0.0
    _priced: bool = False

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

    def record_usage(self, usage: Usage, model: ModelConfig | None = None) -> None:
        self.total_usage = self.total_usage + usage
        if model is not None:
            self.cost += (usage.prompt_tokens / 1_000_000 * model.input_cost
                          + usage.completion_tokens / 1_000_000 * model.output_cost)
            self._priced = True

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.total_usage = Usage()
        self.turns = 0
        self.cost = 0.0
        self._priced = False

    def estimated_cost(self, model: ModelConfig) -> float:
        """Cost so far. Priced per answering model once anything has been
        recorded with one; `model` only prices usage restored from disk."""
        if self._priced:
            return self.cost
        return (
            self.total_usage.prompt_tokens / 1_000_000 * model.input_cost
            + self.total_usage.completion_tokens / 1_000_000 * model.output_cost
        )
