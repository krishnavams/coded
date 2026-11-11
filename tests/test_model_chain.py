"""Model chains: cooldown after failover, sizing for a smaller fallback, stream
retry, per-model timeout, and warnings for unknown aliases."""

from __future__ import annotations

import json

import httpx
import openai

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.llm import LLMClient, build_openai_client
from coded.permissions import PermissionManager

USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _model(base_url, name, **kw):
    return ModelConfig(name=name, model=name, provider="openai-compatible",
                       base_url=base_url, api_key="test", **kw)


def _ok(text):
    return 200, "application/json", json.dumps({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": USAGE})


def _down(method, path, body):
    return 503, "application/json", '{"error": {"message": "down"}}'


def test_failed_primary_is_skipped_during_cooldown(route_server):
    hits = {"main": 0}

    def bad(method, path, body):
        hits["main"] += 1
        return _down(method, path, body)

    with route_server(bad) as primary, route_server(lambda *a: _ok("backup")) as backup:
        main = _model(primary.base + "/v1", "main", max_retries=1, retry_base_delay=0,
                      fallback_cooldown=60)
        client = LLMClient(main, fallbacks=[_model(backup.base + "/v1", "backup")])
        now = [1000.0]
        client._clock = lambda: now[0]
        msgs = [{"role": "user", "content": "hi"}]
        assert client.next_model().name == "main"
        assert client.complete(msgs, stream=False).model == "backup"
        assert hits["main"] == 2  # one try + one retry
        assert client.next_model().name == "backup"
        assert client.complete(msgs, stream=False).model == "backup"
        assert hits["main"] == 2  # not tried again while cooling down
        now[0] += 61
        assert client.next_model().name == "main"
        client.complete(msgs, stream=False)
        assert hits["main"] == 4  # tried again after the cooldown


def test_cooldown_zero_always_tries_primary_first(route_server):
    hits = {"main": 0}

    def bad(method, path, body):
        hits["main"] += 1
        return _down(method, path, body)

    with route_server(bad) as primary, route_server(lambda *a: _ok("backup")) as backup:
        main = _model(primary.base + "/v1", "main", max_retries=0, fallback_cooldown=0)
        client = LLMClient(main, fallbacks=[_model(backup.base + "/v1", "backup")])
        for _ in range(2):
            client.complete([{"role": "user", "content": "hi"}], stream=False)
    assert hits["main"] == 2


def test_cooling_primary_is_still_tried_when_fallback_fails(route_server):
    state = {"main_ok": False}

    def main_route(method, path, body):
        return _ok("main") if state["main_ok"] else _down(method, path, body)

    def backup_route(method, path, body):
        return _down(method, path, body) if state["main_ok"] else _ok("backup")

    with route_server(main_route) as primary, route_server(backup_route) as backup:
        main = _model(primary.base + "/v1", "main", max_retries=0)
        alt = _model(backup.base + "/v1", "backup", max_retries=0)
        client = LLMClient(main, fallbacks=[alt])
        msgs = [{"role": "user", "content": "hi"}]
        assert client.complete(msgs, stream=False).model == "backup"
        state["main_ok"] = True  # backup dies, main recovers (still in cooldown)
        assert client.complete(msgs, stream=False).model == "main"


def test_agent_fits_history_to_a_smaller_fallback(tmp_path, route_server):
    small_window = 8000
    seen = {"too_big": 0, "summaries": 0, "answers": 0}

    def backup_route(method, path, body):
        req = json.loads(body)
        chars = sum(len(m.get("content") or "") for m in req["messages"])
        if chars > small_window * 4:
            seen["too_big"] += 1
            return 400, "application/json", json.dumps(
                {"error": {"message": "maximum context length is 8000 tokens"}})
        if req["messages"][0]["content"].startswith("You compress"):
            seen["summaries"] += 1
            return _ok("- summary of earlier work")
        seen["answers"] += 1
        return _ok("done on backup")

    with route_server(_down) as primary, route_server(backup_route) as backup:
        cfg = Config()
        big = _model(primary.base + "/v1", "big", context_window=300_000, max_retries=0,
                     fallback=["small"], prompt_profile="compact", repo_overview=False)
        small = _model(backup.base + "/v1", "small", context_window=small_window)
        cfg.add_model(big, make_default=True)
        cfg.add_model(small)
        agent = create_agent(model=big, config=cfg, cwd=str(tmp_path),
                             permissions=PermissionManager(auto_approve=True), stream=False,
                             verbose=False)
        # A history that suits 300k but is ~4x the fallback's window.
        for i in range(12):
            agent.session.messages.append({"role": "user", "content": f"q{i} " + "x" * 5000})
            agent.session.messages.append({"role": "assistant", "content": "a" * 5000})
        assert agent.limits.read_lines > 2000
        result = agent.run("go")
        assert result == "done on backup"
        assert seen["summaries"] >= 2  # the history had to be summarized in parts
        assert seen["answers"] == 1
        assert seen["too_big"] <= 1    # at most the request that revealed the problem
        assert agent._sized_model is small and agent.limits.read_lines < 2000


def test_stream_broken_before_any_text_is_retried(monkeypatch):
    client = LLMClient(_model("http://127.0.0.1:9/v1", "m", retry_base_delay=0))
    client._sleep = lambda s: None
    calls = []

    class _Delta:
        def __init__(self, content):
            self.content, self.tool_calls = content, None

    class _Choice:
        def __init__(self, content):
            self.delta, self.finish_reason = _Delta(content), None

    class _Chunk:
        usage = None

        def __init__(self, content):
            self.choices = [_Choice(content)]

    def broken_then_ok(endpoint, params):
        calls.append(1)

        def gen():
            if len(calls) == 1:
                raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
            yield _Chunk("hello")
        return gen()

    monkeypatch.setattr(client, "_create", broken_then_ok)
    shown = []
    out = client.complete([{"role": "user", "content": "hi"}], stream=True, on_text=shown.append)
    assert out.content == "hello" and shown == ["hello"] and len(calls) == 2


def test_stream_broken_after_text_is_not_replayed(monkeypatch):
    client = LLMClient(_model("http://127.0.0.1:9/v1", "m", retry_base_delay=0))
    client._sleep = lambda s: None
    calls = []

    class _Chunk:
        usage = None

        def __init__(self):
            delta = type("D", (), {"content": "partial", "tool_calls": None})()
            self.choices = [type("C", (), {"delta": delta, "finish_reason": None})()]

    def breaks_midway(endpoint, params):
        calls.append(1)

        def gen():
            yield _Chunk()
            raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
        return gen()

    monkeypatch.setattr(client, "_create", breaks_midway)
    shown = []
    try:
        client.complete([{"role": "user", "content": "hi"}], stream=True, on_text=shown.append)
        raise AssertionError("expected the error to surface")
    except openai.APIConnectionError:
        pass
    assert shown == ["partial"] and len(calls) == 1


def test_per_model_timeout_reaches_the_client():
    client = build_openai_client(_model("http://127.0.0.1:9/v1", "m", timeout=45))
    assert client.timeout.read == 45 and client.timeout.connect == 30
    default = build_openai_client(_model("http://127.0.0.1:9/v1", "m"))
    assert default.timeout.read == 600


def test_unknown_aliases_are_reported():
    cfg = Config()
    cfg.add_model(ModelConfig(name="large", model="l", fallback=["large-2", "lrage", "large"]))
    cfg.add_model(ModelConfig(name="large-2", model="l2"))
    cfg.model_roles = {"review": "large-2", "compact": "smal"}
    warnings = "\n".join(cfg.model_warnings())
    assert "'lrage'" in warnings and "'smal'" in warnings and "lists itself" in warnings
    assert "'large-2' is not" not in warnings and len(cfg.model_warnings()) == 3
    cfg.model_roles = {}
    cfg.models["large"].fallback = ["large-2"]
    assert cfg.model_warnings() == []
