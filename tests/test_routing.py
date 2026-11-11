"""Difficulty routing and model pools.

Routing picks the model for a request from the text of the request; pools put
several interchangeable models behind one name. Both are off by default, so the
first test pins that nothing changes without configuration.
"""

from __future__ import annotations

import json

from coded import llm as llm_mod
from coded import routing
from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.routing import RoutingSettings, classify

USAGE = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}


def _ok(text="done"):
    return 200, "application/json", json.dumps({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": USAGE})


def _model(base, name, **kw):
    return ModelConfig(name=name, model=name, provider="openai-compatible",
                       base_url=base, api_key="k", **kw)


def _agent(tmp_path, cfg, model, pool=None, **kw):
    kw.setdefault("stream", False)
    return create_agent(model=model, config=cfg, cwd=str(tmp_path),
                        permissions=PermissionManager(auto_approve=True),
                        verbose=False, pool=pool, **kw)


def _setup(tmp_path, srv, routing_cfg=None, pools=None):
    cfg = Config()
    small = _model(srv.base + "/v1", "small", context_window=32_000, prompt_profile="compact",
                   repo_overview=False)
    medium = _model(srv.base + "/v1", "medium", context_window=128_000, repo_overview=False)
    large = _model(srv.base + "/v1", "large", context_window=300_000, prompt_profile="compact",
                   repo_overview=False)
    for m in (small, medium, large):
        cfg.add_model(m)
    cfg.default_model = "medium"
    cfg.routing = routing_cfg or {}
    cfg.pools = pools or {}
    return cfg, small, medium, large


# ---------------------------------------------------------------------------
# the classifier (pure)
# ---------------------------------------------------------------------------
def test_classifier_needs_evidence_and_leaves_follow_ups_alone():
    assert classify("Refactor the auth system across the codebase") == "hard"
    assert classify("why does the worker deadlock under load?") == "hard"
    assert classify("Implement a retry for the upload endpoint") == "medium"
    assert classify("fix the typo in README") == "simple"
    # Follow-ups carry no evidence: the caller keeps the model it has.
    for text in ("Fix it", "yes", "Add both", "now the tests", ""):
        assert classify(text) is None, text
    # A long brief is a big task whatever words it uses.
    assert classify("Please update the file. " * 40) == "hard"


def test_custom_rules_win_over_the_heuristic():
    settings = RoutingSettings(enabled=True, rules=[
        {"match": r"(?i)^deploy\b", "level": "hard"},
        {"match": "[", "level": "hard"},  # invalid regex: skipped, not fatal
    ])
    assert classify("deploy to staging", settings) == "hard"
    assert classify("fix the typo", settings) == "simple"


# ---------------------------------------------------------------------------
# routing in the agent
# ---------------------------------------------------------------------------
def test_routing_is_off_until_configured(tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, _small, medium, _large = _setup(tmp_path, srv)
        agent = _agent(tmp_path, cfg, medium)
        agent.run("fix the typo in README")
        agent.run("Refactor the auth system across the codebase")
    assert served == ["medium", "medium"]  # nothing moved


def test_routing_picks_a_model_per_request_and_follow_ups_stay(tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, _s, medium, _l = _setup(tmp_path, srv, {
            "enabled": True, "simple": "small", "medium": "medium", "hard": "large",
            "sticky": False})
        agent = _agent(tmp_path, cfg, medium)
        notes = []
        agent._status_note = notes.append
        agent.run("fix the typo in README")                      # simple
        agent.run("Implement a retry for the upload endpoint")    # medium
        agent.run("Refactor the auth system across the codebase")  # hard
        agent.run("now do it")                                    # follow-up: stays
    assert served == ["small", "medium", "large", "large"]
    assert any("Routing: simple" in n for n in notes)
    assert any("Routing: hard" in n for n in notes)


def test_sticky_routing_does_not_drop_back_down(tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, _s, medium, _l = _setup(tmp_path, srv, {
            "enabled": True, "simple": "small", "hard": "large"})  # sticky defaults to on
        agent = _agent(tmp_path, cfg, medium)
        agent.run("Refactor the auth system across the codebase")
        agent.run("fix the typo in README")
    assert served == ["large", "large"]


def test_routing_never_summarizes_history_to_use_a_smaller_model(tmp_path, route_server):
    """A cheap-looking follow-up must not cost the conversation."""
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, _s, _m, large = _setup(tmp_path, srv, {
            "enabled": True, "simple": "small", "hard": "large", "sticky": False})
        agent = _agent(tmp_path, cfg, large)
        for i in range(12):  # ~240k chars: fits 300k, nowhere near 32k
            agent.session.messages.append({"role": "user", "content": f"q{i} " + "x" * 10_000})
            agent.session.messages.append({"role": "assistant", "content": "a" * 10_000})
        before = len(agent.session.messages)
        agent.run("fix the typo in README")
    assert served == ["large"]
    assert len(agent.session.messages) == before + 2  # nothing summarized away
    assert agent.session.compactions == 0


def test_a_refused_downgrade_stays_on_the_current_model_not_main(tmp_path, route_server):
    """Main is smaller than the routed model: 'stay' must mean stay, not go home."""
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, _s, medium, _l = _setup(tmp_path, srv, {
            "enabled": True, "simple": "small", "hard": "large", "sticky": False})
        agent = _agent(tmp_path, cfg, medium)          # main model is the 128k one
        agent.run("Refactor the auth system across the codebase")   # → large (300k)
        for i in range(10):  # history that fits large but neither medium nor small
            agent.session.messages.append({"role": "user", "content": f"q{i} " + "x" * 12_000})
            agent.session.messages.append({"role": "assistant", "content": "a" * 12_000})
        before = len(agent.session.messages)
        agent.run("fix the typo in README")            # simple, but nothing smaller fits
    assert served == ["large", "large"]
    assert len(agent.session.messages) == before + 2 and agent.session.compactions == 0


def test_plan_approval_returns_to_the_routed_model_for_an_unreadable_request(
        tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, _s, medium, _l = _setup(tmp_path, srv, {"enabled": True, "hard": "large"})
        cfg.model_roles = {"plan": "small"}
        agent = _agent(tmp_path, cfg, medium)
        agent.set_mode("plan")
        agent.run("plan it")                 # unreadable: stays on the main model…
        assert agent.model.name == "small"   # …but plan mode borrows the plan model
        agent.set_mode("default")            # exit_plan_mode mid-turn
        assert agent.model.name == "medium"  # not stranded on the plan model
    assert served == ["small"]


def test_an_expanded_template_is_not_classified_as_the_users_words(tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, _s, medium, _l = _setup(tmp_path, srv, {"enabled": True, "hard": "large"})
        agent = _agent(tmp_path, cfg, medium)
        agent.run("Please review this. " * 60, route_hint="/ship")  # long template body
    assert served == ["medium"] and agent._route_level is None


def test_an_explicit_model_pins_it_and_route_on_restores(tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, small, medium, _l = _setup(tmp_path, srv, {
            "enabled": True, "simple": "small", "hard": "large"})
        agent = _agent(tmp_path, cfg, medium)
        agent.switch_model(small)                     # like /model small
        assert not agent.routing_on
        agent.run("Refactor the auth system across the codebase")
        agent.set_routing(True)                       # like /route on
        agent.run("Refactor the auth system across the codebase")
    assert served == ["small", "large"]


def test_a_commands_model_beats_routing_for_one_request(tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, small, medium, _l = _setup(tmp_path, srv, {
            "enabled": True, "simple": "small", "hard": "large"})
        agent = _agent(tmp_path, cfg, medium)
        agent.force_model(small)
        agent.run("Refactor the auth system across the codebase")  # forced → small
        agent.run("Refactor the auth system across the codebase")  # routing again
    assert served == ["small", "large"]


def test_plan_mode_and_approval_keep_the_routed_model(tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, _s, medium, _l = _setup(tmp_path, srv, {
            "enabled": True, "medium": "medium", "hard": "large"})
        cfg.model_roles = {"plan": "small"}
        agent = _agent(tmp_path, cfg, medium)
        agent.set_mode("plan")
        agent.run("Refactor the auth system across the codebase")
        assert agent.model.name == "small"            # planning on the plan model
        agent.set_mode("default")                     # exit_plan_mode mid-turn
        assert agent.model.name == "large"            # back to the routed model
        agent.run("carry on")
    assert served == ["small", "large"]


def test_routing_skips_a_model_that_cannot_do_the_work(tmp_path, route_server):
    with route_server(lambda *a: _ok()) as srv:
        cfg, small, medium, _l = _setup(tmp_path, srv, {
            "enabled": True, "simple": "small", "hard": "large"})
        small.supports_tools = False
        agent = _agent(tmp_path, cfg, medium)
        agent.run("fix the typo in README")
        assert agent.model.name == "medium"           # not the tool-less model

        small.supports_tools = True
        small.supports_vision = False
        agent2 = _agent(tmp_path, cfg, medium)
        agent2._select_turn_model("fix the typo in README", images=["a.png"])
        assert agent2.model.name == "medium"          # images need vision


# ---------------------------------------------------------------------------
# pools
# ---------------------------------------------------------------------------
def test_a_pool_name_works_wherever_a_model_does(tmp_path, route_server):
    with route_server(lambda *a: _ok()) as srv:
        cfg, _s, _m, _l = _setup(tmp_path, srv, pools={"big": ["medium", "large"]})
        routing.reset_pools(seed=1)
        picked = {cfg.get_model("big").name for _ in range(6)}
        assert picked == {"medium", "large"}
        assert cfg.is_pool("big") and not cfg.is_pool("medium")


def test_an_agent_keeps_its_member_while_sub_agents_spread(tmp_path, route_server):
    """The main agent stays put (prompt cache); each sub-agent gets its own member."""
    main_models, sub_models = [], []
    steps = {"n": 0}

    def route(method, path, body):
        req = json.loads(body)
        if req["messages"][0]["content"].startswith("You are a focused sub-agent"):
            sub_models.append(req["model"])
            return _ok("sub done")
        main_models.append(req["model"])
        steps["n"] += 1
        if steps["n"] % 2 == 1:  # first step of each turn: spawn a sub-agent
            return 200, "application/json", json.dumps({
                "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "", "tool_calls": [
                        {"id": f"t{steps['n']}", "type": "function",
                         "function": {"name": "task", "arguments": '{"prompt": "go"}'}}]}}],
                "usage": USAGE})
        return _ok()

    with route_server(route) as srv:
        cfg, _s, medium, large = _setup(tmp_path, srv, pools={"big": ["medium", "large"]})
        cfg.model_roles = {"subagent": "big"}
        routing.reset_pools(seed=5)
        agent = _agent(tmp_path, cfg, medium, pool="big")
        for _ in range(4):
            agent.run("do the thing")
    assert set(main_models) == {"medium"}          # never moved
    assert set(sub_models) == {"medium", "large"}  # spread across the pool


def test_a_pool_as_a_routing_target_does_not_rotate_every_request(tmp_path, route_server):
    served = []

    def route(method, path, body):
        served.append(json.loads(body)["model"])
        return _ok()

    with route_server(route) as srv:
        cfg, _s, medium, large = _setup(tmp_path, srv, {
            "enabled": True, "hard": "big"}, pools={"big": ["medium", "large"]})
        routing.reset_pools(seed=2)
        agent = _agent(tmp_path, cfg, medium)
        agent.run("Refactor the auth system across the codebase")
        agent.run("Refactor the session handling across the codebase")
    assert served[0] == served[1]  # same member, so the prompt cache survives


def test_pool_siblings_are_fallbacks_and_a_dead_member_is_skipped(tmp_path, route_server):
    hits = {"a": 0, "b": 0}

    def dead(method, path, body):
        hits["a"] += 1
        return 503, "application/json", '{"error": {"message": "down"}}'

    def alive(method, path, body):
        hits["b"] += 1
        return _ok()

    llm_mod.reset_endpoints()
    with route_server(dead) as bad, route_server(alive) as good:
        cfg = Config()
        a = _model(bad.base + "/v1", "a", max_retries=0, repo_overview=False)
        b = _model(good.base + "/v1", "b", repo_overview=False)
        cfg.add_model(a)
        cfg.add_model(b)
        cfg.pools = {"pair": ["a", "b"]}
        routing.reset_pools(seed=0)
        agent = _agent(tmp_path, cfg, a, pool="pair")
        assert agent.run("go") == "done"             # fell over to the sibling
        assert hits["b"] == 1
        # The next pick skips the member that just failed — no retry cycle.
        assert cfg.pick_from_pool("pair") == "b"
        assert hits["a"] == 1
    llm_mod.reset_endpoints()


def test_endpoint_state_survives_a_model_switch(tmp_path, route_server):
    """Switching model must not relearn what an endpoint rejects."""
    bodies = []

    def route(method, path, body):
        req = json.loads(body)
        bodies.append(req)
        if "stream_options" in req:
            return 400, "application/json", '{"error": {"message": "stream_options unsupported"}}'
        return _ok()

    llm_mod.reset_endpoints()
    with route_server(route) as srv:
        cfg, small, medium, _l = _setup(tmp_path, srv)
        agent = _agent(tmp_path, cfg, medium, stream=True)
        agent.run("one")
        agent.switch_model(small)
        agent.switch_model(medium)
        agent.run("two")
    rejected = [b for b in bodies if "stream_options" in b]
    assert len(rejected) == 1  # learned once, remembered across the switch
    llm_mod.reset_endpoints()


# ---------------------------------------------------------------------------
# config plumbing
# ---------------------------------------------------------------------------
def test_pools_and_routing_load_from_a_config_file(tmp_path):
    from coded.config import load_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "models": {"a": {"base_url": "http://x/v1", "model": "a"},
                   "b": {"base_url": "http://x/v1", "model": "b", "context_window": 200000}},
        "pools": {"pair": ["a", "b"], "typo": ["nope"], "empty": [],
                  "a": ["b"]},
        "routing": {"enabled": True, "hard": "pair", "simple": "ghost"},
    }))
    cfg = load_config(cwd=str(tmp_path), config_path=str(path), plugins=False)
    assert cfg.pool_members("pair") == ["a", "b"]
    assert cfg.routing_settings().configured and cfg.routing_settings().hard == "pair"
    warnings = "\n".join(cfg.model_warnings())
    assert "'nope' is not a configured model" in warnings
    assert "'empty' is empty" in warnings
    assert "same name as a model" in warnings          # pool "a" clashes
    assert "routing.simple points to 'ghost'" in warnings
    assert "mixes context windows" in warnings         # 128k + 200k


def test_cost_follows_the_model_that_answered(tmp_path, route_server):
    with route_server(lambda *a: _ok()) as srv:
        cfg = Config()
        cheap = _model(srv.base + "/v1", "cheap", input_cost=1.0, output_cost=2.0,
                       repo_overview=False)
        pricey = _model(srv.base + "/v1", "pricey", input_cost=10.0, output_cost=20.0,
                        repo_overview=False)
        cfg.add_model(cheap)
        cfg.add_model(pricey)
        agent = _agent(tmp_path, cfg, cheap)
        agent.run("one")                       # 1000 in + 100 out on cheap
        agent.switch_model(pricey)
        agent.run("two")                       # same usage on pricey
    expected = (1000 / 1e6 * 1 + 100 / 1e6 * 2) + (1000 / 1e6 * 10 + 100 / 1e6 * 20)
    assert abs(agent.session.estimated_cost(agent.model) - expected) < 1e-9
