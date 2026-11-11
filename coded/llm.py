"""OpenAI-compatible LLM client with streaming, tool calling, retries and fallback."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

import httpx
import openai
from openai import OpenAI

from coded.config import ConfigError, ModelConfig

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
    extra: Dict[str, Any] = {}
    if model.timeout:
        # Read timeout applies between stream chunks, so a slow but live model is fine.
        extra["timeout"] = httpx.Timeout(float(model.timeout), connect=min(float(model.timeout), 30.0))
    return OpenAI(
        **extra,
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


def _has_images(messages: List[Dict[str, Any]]) -> bool:
    """Does this request carry an image part (so only a vision model will do)?"""
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "image_url" for part in content):
            return True
    return False


def _is_server_error(exc: Exception) -> bool:
    return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500


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
        self.down_until = 0.0  # monotonic time until which this endpoint is skipped


# Endpoints are shared process-wide: switching model (routing, pools, /model) or
# spawning a sub-agent must not throw away what we learned about an endpoint —
# which optional params it rejects, and whether it is currently down — nor leak
# an HTTP client per switch.
_ENDPOINTS: Dict[tuple, _Endpoint] = {}
_ENDPOINTS_LOCK = threading.Lock()


def _endpoint_key(model: ModelConfig) -> tuple:
    try:
        base = model.resolved_base_url()
    except Exception:  # noqa: BLE001 - keyed by what we have
        base = ""
    return (model.name, base, model.model)


def endpoint_for(model: ModelConfig) -> _Endpoint:
    """The shared endpoint for this model alias (created on first use)."""
    key = _endpoint_key(model)
    with _ENDPOINTS_LOCK:
        endpoint = _ENDPOINTS.get(key)
        if endpoint is None:
            endpoint = _Endpoint(model)
            _ENDPOINTS[key] = endpoint
        return endpoint


def reset_endpoints() -> None:
    """Drop all cached endpoints (tests)."""
    with _ENDPOINTS_LOCK:
        _ENDPOINTS.clear()


class LLMClient:
    """Thin wrapper over the OpenAI SDK pointed at any compatible endpoint."""

    def __init__(self, model: ModelConfig, fallbacks: Optional[List[ModelConfig]] = None,
                 on_status: Optional[Callable[[str], None]] = None,
                 last_resort: Optional[List[ModelConfig]] = None):
        self.model = model
        self._primary = endpoint_for(model)
        self.client = self._primary.client
        self._fallback_models = list(fallbacks or [])
        # Models nobody asked for, tried only when everything declared has
        # failed — better than ending the turn with an error (Config.auto_fallback).
        self._last_resort = [m for m in (last_resort or [])
                             if m.name not in {x.name for x in self._fallback_models}
                             and m.name != model.name]
        self.on_status = on_status
        self._sleep = time.sleep
        self._clock = time.monotonic

    def _chain(self, *, tools: bool = False, images: bool = False,
               size: int = 0) -> List[ModelConfig]:
        """Primary + fallbacks (+ last resort), with endpoints that recently
        failed moved to the end.

        Only the *last-resort* models are filtered — what the user declared is
        honoured as written. A model nobody asked for is skipped when it cannot
        serve this request: no vision for a request with an image, no tools for
        a turn that needs them, and no window too small for the conversation,
        which would otherwise summarize the session to survive an outage.
        """
        def usable(m: ModelConfig) -> bool:
            if images and not m.supports_vision:
                return False
            if tools and not m.supports_tools:
                return False
            return not (size and (m.context_window or 128_000) < size)

        chain = [self.model, *self._fallback_models,
                 *[m for m in self._last_resort if usable(m)]]
        now = self._clock()
        up = [m for m in chain if self._down_until(m) <= now]
        return up + [m for m in chain if self._down_until(m) > now]

    def _down_until(self, model: ModelConfig) -> float:
        return endpoint_for(model).down_until

    def min_context_window(self) -> int:
        """Smallest window in the declared chain: a request this size fits
        whichever answers. Last-resort models are excluded on purpose — one
        small local model should not shrink every request; `_sync_sizing`
        shrinks the history if one of them actually takes over."""
        return min((m.context_window or 128_000) for m in [self.model, *self._fallback_models])

    def next_model(self, size: int = 0) -> ModelConfig:
        """The model the next request goes to first (a fallback while the primary
        is cooling down). Callers size the request for this model's window.

        `size` is the conversation's estimated tokens: without it a cooling
        primary can hand sizing over to a small last-resort model, and the
        caller would summarize the session to fit a model nobody asked for.
        """
        return self._chain(size=size)[0]

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
        from coded.compaction import estimate_tokens

        # A last-resort model must hold the conversation as it stands; never
        # compact a session just to survive someone else's outage.
        size = int(estimate_tokens(messages) * 1.25)
        chain = self._chain(tools=bool(tools and self.model.supports_tools),
                            images=_has_images(messages), size=size)
        declared = {self.model.name, *(m.name for m in self._fallback_models)}
        primary_exc: Optional[Exception] = None
        tried: List[str] = []
        unreachable: set = set()
        for i, model in enumerate(chain):
            if model.name not in declared and model.resolved_base_url_safe() in unreachable:
                continue  # that endpoint did not answer at all; its other models won't either
            try:
                endpoint = self._primary if model is self.model else self._endpoint(model)
            except Exception as exc:  # noqa: BLE001 - e.g. missing key for a fallback
                last_exc = exc
                continue
            tried.append(model.name)
            if model.name not in declared and model.max_retries:
                # One attempt each: a sweep of every configured model must not
                # multiply the backoff the user already waited through.
                model = replace(model, max_retries=0)
            if last_exc is not None:
                how = ("Falling back to model" if model.name in declared
                       else "Everything configured for this model failed; trying")
                self._status(f"{how} '{model.name}' after: {_short(last_exc)}")
            try:
                completion = self._complete_on(endpoint, messages, tools, stream, on_text,
                                               on_reasoning, model)
                completion.model = model.name
                endpoint.down_until = 0.0
                return completion
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if primary_exc is None:
                    primary_exc = exc
                if isinstance(exc, openai.APIConnectionError):
                    unreachable.add(model.resolved_base_url_safe())
                try:
                    exc.coded_model = model  # which model/window the error refers to
                except Exception:  # noqa: BLE001
                    pass
                if not (_is_retryable(exc) or isinstance(exc, (openai.AuthenticationError,
                                                                 openai.PermissionDeniedError,
                                                                 openai.NotFoundError))):
                    raise
                cooldown = float(model.fallback_cooldown or 0)
                if cooldown > 0 and i + 1 < len(chain) and not getattr(exc, "coded_partial", False):
                    endpoint.down_until = self._clock() + cooldown
                    self._status(f"Model '{model.name}' is unavailable; skipping it for "
                                 f"{cooldown:.0f}s.")
        # Report the model the user chose, not whichever substitute failed last:
        # "qwen-coder-7b returned 500" is bewildering when you asked for another.
        failure = primary_exc if primary_exc is not None else last_exc
        assert failure is not None
        if len(tried) > 1:
            try:
                failure.coded_also_tried = [n for n in tried if n != self.model.name]
            except Exception:  # noqa: BLE001
                pass
        raise failure

    def _endpoint(self, model: ModelConfig) -> _Endpoint:
        return endpoint_for(model)

    def _status(self, text: str) -> None:
        if self.on_status:
            self.on_status(text)

    def _params(self, endpoint: _Endpoint, messages, tools, stream: bool,
                model: Optional[ModelConfig] = None) -> Dict[str, Any]:
        model = model or endpoint.model
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

    def _complete_on(self, endpoint, messages, tools, stream, on_text, on_reasoning,
                     model: Optional[ModelConfig] = None) -> Completion:
        # The endpoint is shared, so its stored ModelConfig is whichever one
        # created it; this request's settings come from `model`.
        model = model or endpoint.model
        if not stream:
            return self._parse_once(
                self._create(endpoint, self._params(endpoint, messages, tools, stream, model),
                             model))
        attempt = 0
        while True:
            emitted = [False]

            def wrap(fn):
                if fn is None:
                    return None

                def inner(chunk, _fn=fn):
                    emitted[0] = True
                    _fn(chunk)
                return inner

            resp = self._create(
                endpoint, self._params(endpoint, messages, tools, stream, model), model)
            try:
                return self._consume_stream(resp, wrap(on_text), wrap(on_reasoning))
            except Exception as exc:  # noqa: BLE001
                # A stream that broke before showing anything can be retried safely;
                # once text has been displayed a retry would print it twice.
                if emitted[0]:
                    try:
                        exc.coded_partial = True  # endpoint was up; don't cool it down
                    except Exception:  # noqa: BLE001
                        pass
                if emitted[0] or not _is_retryable(exc) or attempt >= model.max_retries:
                    raise
                delay = min(_MAX_RETRY_DELAY,
                            model.retry_base_delay * (2 ** attempt) * (0.5 + random.random()))
                attempt += 1
                self._status(f"Stream interrupted ({_short(exc)}) — retrying in {delay:.1f}s "
                             f"(attempt {attempt}/{model.max_retries})")
                self._sleep(delay)

    def _create(self, endpoint: _Endpoint, params: Dict[str, Any],
                model: Optional[ModelConfig] = None):
        """Create a completion, retrying transient errors with jittered backoff and
        dropping optional params the endpoint rejects."""
        model = model or endpoint.model
        attempt = 0
        dropped: List[str] = []
        while True:
            try:
                resp = endpoint.client.chat.completions.create(**params)
                if dropped:  # the bare request worked, so the endpoint dislikes these
                    endpoint.unsupported.update(dropped)
                    self._status("That worked, so this endpoint does not support "
                                 + ", ".join(dropped) + "; coded will stop sending them.")
                return resp
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
                    # Some gateways answer an optional param they don't implement
                    # with a 500 instead of a 400, which looks exactly like an
                    # outage. Before giving up, try once without them. `tools` is
                    # never dropped: an agent that silently loses its tools is
                    # worse than an error.
                    if not dropped and _is_server_error(exc):
                        dropped = [n for n in _OPTIONAL_PARAMS if params.pop(n, None) is not None]
                        if dropped:
                            self._status(f"{_short(exc)} — retrying once without "
                                         + ", ".join(dropped))
                            continue
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


def explain_failure(exc: Exception) -> str:
    """A readable, actionable line for an error that ended a turn.

    The raw SDK text ("InternalServerError: Error code: 500 - An unexpected error
    occurred") says nothing about which model or endpoint failed, or what to do
    about it, so say both.
    """
    if isinstance(exc, ConfigError):
        return str(exc)
    model = getattr(exc, "coded_model", None)
    alias = getattr(model, "name", None)
    where = ""
    if model is not None:
        try:
            where = f" ({model.model} at {model.resolved_base_url()})"
        except Exception:  # noqa: BLE001
            where = f" ({model.model})"
    status = getattr(exc, "status_code", None)
    head = f"Model '{alias}'{where} " if alias else "The model "
    body = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__

    also = getattr(exc, "coded_also_tried", None)
    swept = (f"\n  Also tried: {', '.join(also)}." if also else "")
    if _is_server_error(exc):
        hint = (f"`coded models --check {alias}` reports which requests this endpoint "
                "accepts; `/model <alias>` switches models." if alias
                else "`coded models --check` reports which requests each endpoint accepts.")
        return f"{head}returned {status}.\n  {body}{swept}\n  {hint}"
    if isinstance(exc, openai.AuthenticationError):
        return (f"{head}rejected the API key ({status}).\n  {body}\n"
                "  Check the key for this model's provider, then retry.")
    if isinstance(exc, openai.NotFoundError):
        return (f"{head}was not found ({status}).\n  {body}\n"
                "  Check the model id and base_url; `coded models` lists what is configured.")
    if isinstance(exc, openai.APIConnectionError):
        return (f"{head}could not be reached.\n  {body}{swept}\n"
                "  Check the base_url, the network, and any proxy or VPN.")
    # Not a transport/model failure: keep the whole message, it is the content.
    full = str(exc) or type(exc).__name__
    return f"{type(exc).__name__}: {full}" if not alias else f"{head}failed.\n  {full}"


_PROBE_TOOL = {
    "type": "function",
    "function": {"name": "ping", "description": "A no-op used to probe tool support.",
                 "parameters": {"type": "object", "properties": {}}},
}


def probe_stages(model: ModelConfig) -> List[tuple]:
    """Send progressively richer requests to `model`'s endpoint, stopping at the
    first one that fails.

    Returns [(label, exception-or-None), ...]. An endpoint that answers a minimal
    request but 500s on a later stage is rejecting that part of the request, not
    failing: gateways that answer an unimplemented parameter with a 500 instead
    of a 400 are indistinguishable from an outage until you ask this way.
    """
    client = build_openai_client(model)
    base: Dict[str, Any] = {"model": model.model, "max_tokens": 16,
                            "messages": [{"role": "user", "content": "Reply with: ok"}]}
    stages: List[tuple] = [
        ("minimal request", dict(base)),
        ("streaming", {**base, "stream": True}),
        ("stream_options", {**base, "stream": True, "stream_options": {"include_usage": True}}),
    ]
    if model.supports_tools:
        stages.append(("tools", {**base, "tools": [_PROBE_TOOL], "tool_choice": "auto"}))
    if model.reasoning_effort:
        stages.append((f"reasoning_effort={model.reasoning_effort}",
                       {**base, "reasoning_effort": model.reasoning_effort}))
    if model.extra_body:
        stages.append(("extra_body", {**base, "extra_body": dict(model.extra_body)}))
    # Each part can pass alone and still fail together, which is the request a
    # real turn sends.
    whole: Dict[str, Any] = {**base, "stream": True,
                             "stream_options": {"include_usage": True},
                             "messages": [{"role": "system", "content": "You are a coding agent."},
                                          *base["messages"]]}
    if model.supports_tools:
        whole.update(tools=[_PROBE_TOOL], tool_choice="auto")
    if model.reasoning_effort:
        whole["reasoning_effort"] = model.reasoning_effort
    if model.extra_body:
        whole["extra_body"] = dict(model.extra_body)
    stages.append(("a full turn (system prompt + streaming"
                   + (" + tools" if model.supports_tools else "") + ")", whole))

    results: List[tuple] = []
    for label, params in stages:
        try:
            resp = client.chat.completions.create(**params)
            if params.get("stream"):
                for _ in resp:  # the failure may only surface while reading
                    pass
            results.append((label, None))
        except Exception as exc:  # noqa: BLE001 - every outcome is a result here
            results.append((label, exc))
            break
    return results


def probe_hint(label: str, exc: Exception) -> str:
    """What to do about the stage that failed."""
    if label == "minimal request":
        return ("even a bare request fails, so this is the endpoint or the model id — "
                "nothing coded sends. Check the model id, base_url and key.")
    if label == "streaming":
        return ("this endpoint cannot stream. Run coded with --no-stream (or set "
                '"stream": false in config.json), or use a different endpoint.')
    if label == "stream_options":
        return ("the endpoint does not implement stream_options; coded now drops it "
                "automatically after a 5xx, so retry the request.")
    if label == "tools":
        return ('this endpoint cannot take tool definitions. Set "supports_tools": false '
                "for this model — coded will then run it without tools.")
    if label.startswith("reasoning_effort"):
        return 'remove "reasoning_effort" from this model.'
    if label == "extra_body":
        return 'remove or correct "extra_body" for this model.'
    if label.startswith("a full turn"):
        return ("each part is accepted on its own but the combination is not — most "
                "often the system prompt plus tools is too large for this endpoint. "
                "Try a smaller prompt profile, or another model.")
    return _short(exc)
