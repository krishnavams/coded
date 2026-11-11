"""The interactive model wizard (`coded config setup`, REPL /setup).

The wizard edits one config file's raw JSON, so these tests assert on the file:
what it keeps, what it never writes, and that an abort changes nothing.
"""

from __future__ import annotations

import json

import pytest

from coded.config import ConfigError
from coded.config_wizard import Prompter, Wizard, dangling_references


def scripted(answers):
    """A Prompter that replays `answers` and records everything printed."""
    said = list(answers)
    printed = []

    def ask(question, _default=""):
        if not said:
            raise AssertionError(f"ran out of answers at: {question!r}\n"
                                 + "\n".join(printed))
        return said.pop(0)

    p = Prompter(ask=ask, secret=lambda q: ask(q), out=lambda t: printed.append(str(t)))
    p.printed = printed  # type: ignore[attr-defined]
    p.remaining = said  # type: ignore[attr-defined]
    return p


ADD_TYPED = [
    "add a model",
    "openai-compatible",                # provider
    "https://gw.example.com/v1",        # base URL
    "env",                              # key source
    "GW_KEY",                           # env var name
    "n",                                # don't ask the endpoint for its models
    "glm-5.3-flash",                    # model id
    "flash",                            # alias
    "128000",                           # context window
    "y",                                # supports tools
    "n",                                # no vision
    "n",                                # don't test it now
]


def test_add_a_model_and_save(tmp_path):
    path = tmp_path / "config.json"
    p = scripted(ADD_TYPED + ["save and quit", "y"])
    assert Wizard(path, p).run() is True

    data = json.loads(path.read_text())
    assert data["models"] == {
        "flash": {"provider": "openai-compatible", "base_url": "https://gw.example.com/v1",
                  "api_key_env": "GW_KEY", "model": "glm-5.3-flash",
                  "context_window": 128000},
    }
    assert data["default_model"] == "flash"
    assert '"api_key"' not in json.dumps(data)  # only api_key_env


def test_the_file_keeps_keys_the_wizard_knows_nothing_about(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "hooks": {"PreToolUse": [{"command": "echo hi"}]},
        "mcp_servers": {"fs": {"command": "mcp-fs"}},
        "models": {"old": {"model": "o", "provider": "openai", "limits": {"read_lines": 500},
                           "prompts_dir": "~/p"}},
        "default_model": "old",
        "mystery": 42,
    }, indent=2))
    p = scripted(ADD_TYPED + ["save and quit", "y"])
    Wizard(path, p).run()

    data = json.loads(path.read_text())
    assert data["hooks"] == {"PreToolUse": [{"command": "echo hi"}]}
    assert data["mcp_servers"] == {"fs": {"command": "mcp-fs"}}
    assert data["mystery"] == 42
    assert data["models"]["old"]["limits"] == {"read_lines": 500}
    assert data["models"]["old"]["prompts_dir"] == "~/p"
    assert data["default_model"] == "old"  # an existing default is not hijacked
    assert set(data["models"]) == {"old", "flash"}


def test_quitting_without_saving_leaves_the_file_untouched(tmp_path):
    path = tmp_path / "config.json"
    before = json.dumps({"models": {"a": {"model": "a"}}, "default_model": "a"}, indent=2)
    path.write_text(before)
    p = scripted(ADD_TYPED + ["quit without saving"])
    assert Wizard(path, p).run() is False
    assert path.read_text() == before


def test_declining_the_write_leaves_the_file_untouched(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    p = scripted(ADD_TYPED + ["save and quit", "n"])
    assert Wizard(path, p).run() is False
    assert path.read_text() == "{}"


def test_a_stored_key_is_never_printed_back(tmp_path):
    path = tmp_path / "config.json"
    p = scripted([
        "add a model", "openai-compatible", "https://gw.example.com/v1",
        "store", "sk-super-secret-value",
        "n", "m", "m", "128000", "y", "n", "n",
        "save and quit", "y",
    ])
    Wizard(path, p).run()
    assert json.loads(path.read_text())["models"]["m"]["api_key"] == "sk-super-secret-value"
    assert "sk-super-secret-value" not in "\n".join(p.printed)  # type: ignore[attr-defined]
    assert path.stat().st_mode & 0o077 == 0  # not readable by anyone else


def test_the_project_file_only_gets_env_var_names(tmp_path):
    """A project's .coded/ is not necessarily gitignored by the user's repo."""
    path = tmp_path / "config.json"
    p = scripted([
        "add a model", "openai-compatible", "https://gw.example.com/v1",
        "store",          # asked for a literal key...
        "GW_KEY",         # ...and gets the env-var question instead
        "n", "m", "m", "128000", "y", "n", "n",
        "save and quit", "y",
    ])
    Wizard(path, p, allow_literal_key=False).run()
    entry = json.loads(path.read_text())["models"]["m"]
    assert "api_key" not in entry and entry["api_key_env"] == "GW_KEY"


def test_removing_a_model_repoints_the_default_and_reports_dangling_refs(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "models": {"a": {"model": "a"}, "b": {"model": "b", "fallback": ["a"]}},
        "default_model": "a",
        "model_roles": {"plan": "a"},
        "pools": {"big": ["a", "b"]},
    }))
    p = scripted(["remove a model", "a", "y", "b", "save and quit", "y"])
    Wizard(path, p).run()
    data = json.loads(path.read_text())
    assert "a" not in data["models"]
    assert data["default_model"] == "b"
    refs = dangling_references(data)
    assert 'model_roles.plan = "a"' in refs
    assert 'pools.big → "a"' in refs
    assert 'models.b.fallback → "a"' in refs


def test_renaming_a_model_follows_every_reference(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "models": {"a": {"model": "a"}, "b": {"model": "b", "fallback": ["a"]}},
        "default_model": "a",
        "model_roles": {"plan": "a"},
        "pools": {"big": ["a", "b"]},
        "routing": {"enabled": True, "hard": "a"},
    }))
    p = scripted(["edit a model", "a", "rename", "fast", "save and quit", "y"])
    Wizard(path, p).run()
    data = json.loads(path.read_text())
    assert set(data["models"]) == {"fast", "b"}
    assert data["default_model"] == "fast"
    assert data["model_roles"]["plan"] == "fast"
    assert data["pools"]["big"] == ["fast", "b"]
    assert data["routing"]["hard"] == "fast"
    assert data["models"]["b"]["fallback"] == ["fast"]
    assert dangling_references(data) == []


def test_invalid_json_is_refused_rather_than_overwritten(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError) as exc:
        Wizard(path, scripted([]))
    assert "not valid JSON" in str(exc.value)
    assert path.read_text() == "{not json"


def test_models_are_discovered_from_the_endpoint(route_server):
    """The common case: one gateway, several models, pick them by number."""
    def route(method, path, body):
        if path.endswith("/models"):
            return 200, "application/json", json.dumps({"object": "list", "data": [
                {"id": "gpt-oss-120b", "object": "model"},
                {"id": "glm-5.3-flash", "object": "model"},
                {"id": "qwen3-14b", "object": "model"}]})
        return 404, "application/json", "{}"

    with route_server(route) as srv:
        from coded.config_wizard import list_endpoint_models

        ids = list_endpoint_models({"provider": "openai-compatible",
                                    "base_url": srv.base + "/v1", "api_key": "k"})
    assert ids == ["glm-5.3-flash", "gpt-oss-120b", "qwen3-14b"]


def test_adding_several_models_from_one_endpoint(route_server, tmp_path):
    def route(method, path, body):
        if path.endswith("/models"):
            return 200, "application/json", json.dumps({"object": "list", "data": [
                {"id": "a"}, {"id": "b"}, {"id": "c"}]})
        return 404, "application/json", "{}"

    path = tmp_path / "config.json"
    with route_server(route) as srv:
        p = scripted([
            "add a model", "openai-compatible", srv.base + "/v1", "env", "GW_KEY",
            "y",            # ask the endpoint
            "1,3",          # pick a and c
            "a", "128000", "y", "n", "n",
            "c", "32000", "y", "n", "n",
            "save and quit", "y",
        ])
        Wizard(path, p).run()
    models = json.loads(path.read_text())["models"]
    assert set(models) == {"a", "c"}
    assert models["c"]["context_window"] == 32000


def test_a_model_is_probed_and_tools_can_be_turned_off(route_server, tmp_path):
    def route(method, path, body):
        params = json.loads(body)
        if "tools" in params:
            return 500, "application/json", '{"error": {"message": "nope"}}'
        if params.get("stream"):
            chunk = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                     "choices": [{"index": 0, "delta": {"content": "ok"},
                                  "finish_reason": "stop"}]}
            return 200, "text/event-stream", f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return 200, "application/json", json.dumps({
            "id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

    path = tmp_path / "config.json"
    with route_server(route) as srv:
        p = scripted([
            "add a model", "openai-compatible", srv.base + "/v1", "env", "GW_KEY",
            "n", "m", "m", "128000", "y", "n",
            "y",     # test it now
            "y",     # tools failed: set supports_tools false and re-probe
            "save and quit", "y",
        ])
        Wizard(path, p).run()
    entry = json.loads(path.read_text())["models"]["m"]
    assert entry["supports_tools"] is False
    out = "\n".join(p.printed)  # type: ignore[attr-defined]
    assert "✗ tools" in out and "every request shape was accepted" in out


def test_roles_pools_and_routing_are_written(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"models": {"big": {"model": "big"}, "small": {"model": "s"}},
                                "default_model": "big"}))
    p = scripted([
        "model pool", "fast", "small",
        "models per role", "small", "small", "small", "(none)", "(none)",
        "difficulty routing", "y", "small", "fast", "big",
        "save and quit", "y",
    ])
    Wizard(path, p).run()
    data = json.loads(path.read_text())
    assert data["pools"] == {"fast": ["small"]}
    assert data["model_roles"] == {"plan": "small", "subagent": "small", "compact": "small"}
    assert data["routing"] == {"enabled": True, "simple": "small", "medium": "fast",
                               "hard": "big"}


def test_setup_needs_a_terminal(monkeypatch, capsys, tmp_path):
    from coded import cli

    monkeypatch.setenv("CODED_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["config", "setup"]) == 1
    assert "needs a terminal" in capsys.readouterr().out


def test_the_wizard_never_writes_another_layers_models(tmp_path, monkeypatch):
    """`load_config` merges user + project + plugins; the wizard must not bake
    any of that into the file it edits."""
    user = tmp_path / "user.json"
    user.write_text(json.dumps({"models": {"mine": {"model": "mine"}}, "default_model": "mine"}))
    project = tmp_path / "proj" / ".coded"
    project.mkdir(parents=True)
    (project / "config.json").write_text(json.dumps({"models": {"theirs": {"model": "t"}}}))

    p = scripted(ADD_TYPED + ["save and quit", "y"])
    Wizard(user, p).run()
    assert set(json.loads(user.read_text())["models"]) == {"mine", "flash"}
