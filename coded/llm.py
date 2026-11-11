"""OpenAI-compatible LLM client with streaming, tool calling, retries and fallback."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx
import openai
from openai import OpenAI

from coded.config import ModelConfig

# Optional request params some endpoints reject; dropped (and remembered) on a 400.
_OPTIONAL_PARAMS = ("reasoning_effort", "stream_options", "parallel_tool_calls")
_MAX_RETRY_DELAY = 60.0


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
    reasoning: str = ""
    model: str = ""  # alias that actually answered (differs after a fallback)


def build_openai_client(model: ModelConfig) -> OpenAI:
    """Construct an OpenAI client honoring the model's TLS settings.

    SDK-level retries are disabled; LLMClient retries itself so it can apply
    one policy (and fall back to another endpoint) consistently.
    """
    verify = model.ssl_verify()
    http_client = httpx.Client(verify=verify)
    return OpenAI(
        base_url=model.resolved_base_url(),
        api_key=model.resolved_api_key(),
        default_headers=model.extra_headers or None,
        http_client=http_client,
        max_retries=0,
    )


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, openai.APIConnectionError):  # includes timeouts
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in (408, 409, 429) or exc.status_code >= 500
    return False


def _retry_after(exc: Exception) -> Optional[float]:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    for name in ("retry-after-ms", "retry-after"):
        val = headers.get(name)
        if val is None:
            continue
        try:
            secs = float(val) / (1000 if name.endswith("-ms") else 1)
        except ValueError:
            continue
        return max(0.0, min(secs, _MAX_RETRY_DELAY))
    return None


_OVERFLOW_PHRASES = ("context length", "context_length", "maximum context", "context window",
                     "too many tokens", "prompt is too long", "request too large",
                     "reduce the length", "exceeds the context")


def is_context_overflow(exc: Exception) -> bool:
    """A 400/413 saying the request is too long for the model's context window."""
    if getattr(exc, "status_code", None) not in (400, 413):
        return False
    msg = str(exc).lower()
    return any(k in msg for k in _OVERFLOW_PHRASES)


def _rejected_param(exc: Exception, params: Dict[str, Any]) -> Optional[str]:
    """If a 400/422 complains about an optional param we sent, return its name."""
    if not isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        return None
    msg = str(exc).lower()
    for name in _OPTIONAL_PARAMS:
        if name in params and (name in msg or name.split("_")[0] in msg):
            return name
    return None


class _Endpoint:
    """One model + its client + the optional params it turned out not to support."""

    def __init__(self, model: ModelConfig):
        self.model = model
        self.client = build_openai_client(model)
        self.unsupported: set = set()


class LLMClient:
    """Thin wrapper over the OpenAI SDK pointed at any compatible endpoint."""

    def __init__(self, model: ModelConfig, fallbacks: Optional[List[ModelConfig]] = None,
                 on_status: Optional[Callable[[str], None]] = None):
        self.model = model
        self._primary = _Endpoint(model)
        self.client = self._primary.client
        self._fallback_models = list(fallbacks or [])
        self._fallbacks: Dict[str, _Endpoint] = {}
        self.on_status = on_status
        self._sleep = time.sleep

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = True,
        on_text: Optional[Callable[[str], None]] = None,
        on_reasoning: Optional[Callable[[str], None]] = None,
    ) -> Completion:
        last_exc: Optional[Exception] = None
        for i, model in enumerate([self.model, *self._fallback_models]):
            try:
                endpoint = self._primary if i == 0 else self._endpoint(model)
            except Exception as exc:  # noqa: BLE001 - e.g. missing key for a fallback
                last_exc = exc
                continue
            if i > 0:
                self._status(f"Falling back to model '{model.name}' after: {_short(last_exc)}")
            try:
                completion = self._complete_on(endpoint, messages, tools, stream, on_text,
                                               on_reasoning)
                completion.model = model.name
                return completion
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not (_is_retryable(exc) or isinstance(exc, (openai.AuthenticationError,
                                                                 openai.PermissionDeniedError,
                                                                 openai.NotFoundError))):
                    raise
        assert last_exc is not None
        raise last_exc

    def _endpoint(self, model: ModelConfig) -> _Endpoint:
        if model.name not in self._fallbacks:
            self._fallbacks[model.name] = _Endpoint(model)
        return self._fallbacks[model.name]

    def _status(self, text: str) -> None:
        if self.on_status:
            self.on_status(text)

    def _params(self, endpoint: _Endpoint, messages, tools, stream: bool) -> Dict[str, Any]:
        model = endpoint.model
        params: Dict[str, Any] = {"model": model.model, "messages": messages}
        if model.max_tokens:
            params["max_tokens"] = model.max_tokens
        if model.temperature is not None:
            params["temperature"] = model.temperature
        if model.reasoning_effort:
            params["reasoning_effort"] = model.reasoning_effort
        if tools and model.supports_tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        if stream:
            params["stream"] = True
            # Ask for usage in the final chunk where supported.
            params["stream_options"] = {"include_usage": True}
        if model.extra_body:
            params["extra_body"] = dict(model.extra_body)
        for name in endpoint.unsupported:
            params.pop(name, None)
        return params

    def _complete_on(self, endpoint, messages, tools, stream, on_text, on_reasoning) -> Completion:
        params = self._params(endpoint, messages, tools, stream)
        resp = self._create(endpoint, params)
        if stream:
            return self._consume_stream(resp, on_text, on_reasoning)
        return self._parse_once(resp)

    def _create(self, endpoint: _Endpoint, params: Dict[str, Any]):
        """Create a completion, retrying transient errors with jittered backoff and
        dropping optional params the endpoint rejects."""
        model = endpoint.model
        attempt = 0
        while True:
            try:
                return endpoint.client.chat.completions.create(**params)
            except Exception as exc:  # noqa: BLE001
                if is_context_overflow(exc):
                    raise  # the agent handles this; it's not an unsupported parameter
                bad = _rejected_param(exc, params)
                if bad is None and "stream_options" in params and isinstance(exc, openai.APIStatusError) \
                        and 400 <= exc.status_code < 500 and exc.status_code not in (401, 403, 408, 409, 429):
                    bad = "stream_options"  # partial OpenAI implementations often choke on it
                if bad is not None:
                    endpoint.unsupported.add(bad)
                    params.pop(bad, None)
                    continue
                if not _is_retryable(exc) or attempt >= model.max_retries:
                    raise
                delay = _retry_after(exc)
                if delay is None:
                    delay = min(_MAX_RETRY_DELAY,
                                model.retry_base_delay * (2 ** attempt) * (0.5 + random.random()))
                attempt += 1
                self._status(f"{_short(exc)} — retrying in {delay:.1f}s "
                             f"(attempt {attempt}/{model.max_retries})")
                self._sleep(delay)

    # -- non-streaming ------------------------------------------------------
    @staticmethod
    def _parse_once(resp) -> Completion:
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = []
        for tc in msg.tool_calls or []:
            tool_calls.append(ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}"))
        usage = Usage()
        if resp.usage:
            usage = Usage(resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
        reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None) or ""
        return Completion(msg.content or "", tool_calls, usage, choice.finish_reason,
                          reasoning if isinstance(reasoning, str) else "")

    # -- streaming ----------------------------------------------------------
    @staticmethod
    def _consume_stream(stream, on_text, on_reasoning) -> Completion:
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
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
            if delta is None:
                continue
            thought = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if isinstance(thought, str) and thought:
                reasoning_parts.append(thought)
                if on_reasoning:
                    on_reasoning(thought)
            if delta.content:
                content_parts.append(delta.content)
                if on_text:
                    on_text(delta.content)
            for tc in delta.tool_calls or []:
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
        return Completion("".join(content_parts), tool_calls, usage, finish_reason,
                          "".join(reasoning_parts))


def _short(exc: Optional[Exception]) -> str:
    if exc is None:
        return "error"
    status = getattr(exc, "status_code", None)
    text = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
    return f"{type(exc).__name__}{f' {status}' if status else ''}: {text}"
