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

    def broken_then_ok(endpoint, params, model=None):
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

    def breaks_midway(endpoint, params, model=None):
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
    assert client._primary.down_until == 0


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


def _failover_agent(tmp_path, primary, backup, small_window, **big_kw):
    cfg = Config()
    big = _model(primary.base + "/v1", "big", context_window=300_000, max_retries=0,
                 fallback=["small"], prompt_profile="compact", repo_overview=False, **big_kw)
    small = _model(backup.base + "/v1", "small", context_window=small_window)
    cfg.add_model(big, make_default=True)
    cfg.add_model(small)
    agent = create_agent(model=big, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(auto_approve=True), stream=False,
                         verbose=False)
    return agent, small


def test_failover_leaves_a_history_that_fits_untouched(tmp_path, route_server):
    with route_server(_down) as primary, route_server(lambda *a: _ok("ok")) as backup:
        agent, small = _failover_agent(tmp_path, primary, backup, 128_000)
        big_result = "line\n" * 12_000  # 60k chars: fine for 128k
        agent.session.messages += [
            {"role": "user", "content": "read it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": big_result},
            {"role": "assistant", "content": "read."},
        ]
        before = json.dumps(agent.session.messages)
        assert agent.run("go") == "ok" and agent.run("more") == "ok"
        assert json.dumps(agent.session.messages[:len(json.loads(before))]) == before
        assert getattr(agent.session, "compactions", 0) == 0
        # Tools now get the fallback's limits.
        from coded.limits import get_limits
        assert agent._sized_model is small and get_limits(agent._ctx()).read_lines == 2000


def test_smaller_fallback_overflow_is_handled_without_cooldown(tmp_path, route_server):
    small_window = 8000

    def backup_route(method, path, body):
        req = json.loads(body)
        if sum(len(m.get("content") or "") for m in req["messages"]) > small_window * 4:
            return 400, "application/json", json.dumps(
                {"error": {"message": "maximum context length is 8000 tokens"}})
        if req["messages"][0]["content"].startswith("You compress"):
            return _ok("- summary")
        return _ok("done")

    with route_server(_down) as primary, route_server(backup_route) as backup:
        agent, small = _failover_agent(tmp_path, primary, backup, small_window,
                                       fallback_cooldown=0)
        for i in range(12):
            agent.session.messages.append({"role": "user", "content": f"q{i} " + "x" * 5000})
            agent.session.messages.append({"role": "assistant", "content": "a" * 5000})
        assert agent.run("go") == "done"
        assert agent.run("again") == "done"  # the overflow retry is available again


def test_compacting_with_keep_recent_zero_keeps_the_current_request():
    from coded.compaction import maybe_compact

    class _LLM:
        model = ModelConfig(name="m", model="m")

        def complete(self, messages, tools=None, stream=False):
            return type("C", (), {"content": "summary"})()

    class _Session:
        compactions = 0
        last_prompt_tokens = 0

    s = _Session()
    s.messages = [{"role": "system", "content": "sys"},
                  {"role": "user", "content": "old"}, {"role": "assistant", "content": "old a"},
                  {"role": "user", "content": "current"},
                  {"role": "assistant", "content": "", "tool_calls": [
                      {"id": "c", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
                  {"role": "tool", "tool_call_id": "c", "content": "files"}]
    assert maybe_compact(s, _LLM.model, _LLM(), force=True, keep_recent=0)
    assert [m["role"] for m in s.messages] == ["system", "user", "user", "assistant", "tool"]
    assert s.messages[2]["content"] == "current"
    assert maybe_compact(s, _LLM.model, _LLM(), force=True, keep_recent=-1)
    assert [m["role"] for m in s.messages] == ["system", "user"]


def test_new_model_fields_load_from_a_config_file(tmp_path):
    from coded.config import load_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"models": {
        "large": {"base_url": "http://x/v1", "model": "l", "timeout": 900,
                  "fallback_cooldown": 30, "fallback": ["nope"]}}}))
    cfg = load_config(cwd=str(tmp_path), config_path=str(path), plugins=False)
    assert cfg.models["large"].timeout == 900 and cfg.models["large"].fallback_cooldown == 30
    assert any("'nope'" in w for w in cfg.model_warnings())


def _two_model_agent(tmp_path, url, roles):
    cfg = Config()
    main = _model(url, "medium")
    big = _model(url, "large", context_window=300_000)
    cfg.add_model(main, make_default=True)
    cfg.add_model(big)
    cfg.model_roles = roles
    agent = create_agent(model=main, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(auto_approve=True), stream=False,
                         verbose=False)
    return agent, main, big


def test_plan_role_model_plans_and_main_model_executes(tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok("ok")

    with route_server(route) as srv:
        agent, main, big = _two_model_agent(tmp_path, srv.base + "/v1", {"plan": "large"})
        agent.set_mode("plan")
        assert agent.model is big and agent.permissions.plan_mode
        assert agent.limits.read_lines > 2000  # sized for the planner
        agent.run("plan it")
        agent.set_mode("default")
        assert agent.model is main and agent.limits.read_lines == 2000
        agent.run("do it")
        assert served == ["large", "medium"]
        # /model while planning changes the main model, which execution returns to.
        agent.set_mode("plan")
        agent.switch_model(big)
        agent.set_mode("default")
        assert agent.model is big


def test_plan_mode_without_a_plan_role_keeps_the_model(tmp_path, route_server):
    with route_server(lambda *a: _ok("ok")) as srv:
        agent, main, _ = _two_model_agent(tmp_path, srv.base + "/v1", {})
        agent.set_mode("plan")
        assert agent.model is main
        agent.set_mode("default")
        assert agent.model is main and not agent.permissions.plan_mode


# -- a 500 that is really an unsupported parameter ---------------------------

def _sse(text):
    chunk = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
             "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
    done = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    body = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(done)}\n\ndata: [DONE]\n\n"
    return 200, "text/event-stream", body


def test_a_500_caused_by_stream_options_is_retried_without_it(route_server):
    """Some gateways answer a parameter they don't implement with a 500, which is
    indistinguishable from an outage until you try the request without it."""
    seen = []

    def route(method, path, body):
        params = json.loads(body)
        seen.append(sorted(k for k in ("stream_options", "reasoning_effort") if k in params))
        if "stream_options" in params:
            return 500, "application/json", '{"error": {"message": "An unexpected error"}}'
        return _sse("hi") if params.get("stream") else _ok("hi")

    with route_server(route) as srv:
        mc = _model(srv.base + "/v1", "m", max_retries=1, retry_base_delay=0)
        client = LLMClient(mc)
        out = client.complete([{"role": "user", "content": "hi"}], stream=True,
                              on_text=lambda t: None)
        assert out.content == "hi"
        # try, one retry, then one attempt with the optional params removed
        assert seen == [["stream_options"], ["stream_options"], []]
        # and the endpoint is remembered, so the next request never sends it again
        client.complete([{"role": "user", "content": "hi"}], stream=True)
        assert seen[-1] == []
        assert len(seen) == 4


def test_a_real_outage_still_fails_after_the_bare_retry(route_server):
    calls = {"n": 0}

    def route(method, path, body):
        calls["n"] += 1
        return 500, "application/json", '{"error": {"message": "down"}}'

    with route_server(route) as srv:
        client = LLMClient(_model(srv.base + "/v1", "m", max_retries=1, retry_base_delay=0))
        try:
            client.complete([{"role": "user", "content": "hi"}], stream=True)
            assert False, "expected the 500 to surface"
        except openai.InternalServerError as exc:
            assert exc.coded_model.name == "m"
            text = __import__("coded.llm", fromlist=["x"]).explain_failure(exc)
            assert "'m'" in text and "500" in text and "coded models --check m" in text
    assert calls["n"] == 3  # try + retry + the one bare attempt


def test_check_fails_on_a_500_that_only_tools_trigger(route_server, capsys):
    """The old check sent one minimal request, so a gateway that 500s on tool
    definitions passed the check and failed every real turn."""
    def route(method, path, body):
        params = json.loads(body)
        if "tools" in params:
            return 500, "application/json", '{"error": {"message": "nope"}}'
        return _sse("ok") if params.get("stream") else _ok("ok")

    from coded.cli import _check_models

    with route_server(route) as srv:
        cfg = Config(models={"m": _model(srv.base + "/v1", "m")}, default_model="m")
        rc = _check_models(cfg, ["m"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "✓ streaming" in out and "✗ tools" in out
    assert "supports_tools" in out


def test_check_passes_when_every_shape_is_accepted(route_server, capsys):
    def route(method, path, body):
        return _sse("ok") if json.loads(body).get("stream") else _ok("ok")

    from coded.cli import _check_models

    with route_server(route) as srv:
        cfg = Config(models={"m": _model(srv.base + "/v1", "m")}, default_model="m")
        rc = _check_models(cfg, ["m"])
    out = capsys.readouterr().out
    assert rc == 0 and "request shapes accepted" in out


def test_probe_reports_the_first_request_shape_the_endpoint_refuses(route_server):
    def route(method, path, body):
        params = json.loads(body)
        if "stream_options" in params:
            return 500, "application/json", '{"error": {"message": "nope"}}'
        return _sse("ok") if params.get("stream") else _ok("ok")

    from coded.llm import probe_hint, probe_stages

    with route_server(route) as srv:
        stages = probe_stages(_model(srv.base + "/v1", "m"))
    labels = [label for label, _ in stages]
    assert labels == ["minimal request", "streaming", "stream_options"]
    assert [exc is None for _, exc in stages] == [True, True, False]
    assert "stream_options" in probe_hint(*stages[-1])


def test_probe_stops_at_a_dead_endpoint(route_server):
    with route_server(_down) as srv:
        from coded.llm import probe_hint, probe_stages

        stages = probe_stages(_model(srv.base + "/v1", "m"))
    assert len(stages) == 1 and stages[0][0] == "minimal request"
    assert "endpoint or the model id" in probe_hint(*stages[0])
