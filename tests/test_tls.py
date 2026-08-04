"""Tests for per-model TLS / certificate configuration."""

from __future__ import annotations

import json

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.llm import LLMClient, build_openai_client
from coded.permissions import PermissionManager


def test_ssl_verify_precedence(monkeypatch):
    monkeypatch.delenv("CODED_CA_BUNDLE", raising=False)
    # Explicit bundle wins.
    m = ModelConfig(name="m", model="m", base_url="http://h/v1", api_key="k", ca_bundle="/tmp/ca.pem")
    assert m.ssl_verify() == "/tmp/ca.pem"
    # Disabled verification.
    assert ModelConfig(name="m", model="m", verify_ssl=False).ssl_verify() is False
    # Default verifies.
    assert ModelConfig(name="m", model="m").ssl_verify() is True


def test_ssl_env_ca_bundle(monkeypatch):
    monkeypatch.setenv("CODED_CA_BUNDLE", "/etc/ssl/corp.pem")
    assert ModelConfig(name="m", model="m").ssl_verify() == "/etc/ssl/corp.pem"
    # An explicit disable is not overridden by the env bundle.
    assert ModelConfig(name="m", model="m", verify_ssl=False).ssl_verify() is False


def test_from_dict_parses_tls_fields():
    m = ModelConfig.from_dict("x", {"base_url": "https://h/v1", "verify_ssl": False,
                                    "ca_bundle": "/tmp/b.pem"})
    assert m.verify_ssl is False and m.ca_bundle == "/tmp/b.pem"


def test_client_builds_with_custom_verify():
    # verify=False must construct a working client (no cert error at build time).
    m = ModelConfig(name="m", model="m", base_url="http://h/v1", api_key="k", verify_ssl=False)
    client = build_openai_client(m)
    assert client is not None


def test_agent_runs_with_verification_disabled(tmp_path, fake_server):
    """End-to-end: a model with verify_ssl=False still drives the loop (http path)."""
    def script(body):
        usage = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
        has_tool = any(m.get("role") == "tool" for m in body.get("messages", []))
        if has_tool:
            return {"role": "assistant", "content": "done"}, usage
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "write",
                                             "arguments": json.dumps({"path": "t.txt", "content": "hi"})}}]}, usage

    with fake_server(script) as server:
        cfg = Config()
        model = ModelConfig(name="fake", model="fake", provider="openai-compatible",
                            base_url=server.base_url, api_key="k", verify_ssl=False)
        cfg.add_model(model, make_default=True)
        agent = create_agent(model=model, config=cfg, cwd=str(tmp_path),
                             permissions=PermissionManager(auto_approve=True),
                             stream=False, verbose=False)
        agent.run("make the file")
    assert (tmp_path / "t.txt").read_text() == "hi"
