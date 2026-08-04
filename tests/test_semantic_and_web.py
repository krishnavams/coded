"""Tests for semantic index/search, web_fetch, web_search, and image encoding."""

from __future__ import annotations

import json

from coded.config import Config, ModelConfig
from coded.embeddings import EmbeddingClient, resolve_embedding_model
from coded.index import CodeIndex
from coded.permissions import PermissionManager
from coded.tools import build_registry
from coded.tools.base import ToolContext


def _embed_client(base_url):
    mc = ModelConfig(name="embedding", model="fake-embed", provider="openai-compatible",
                     base_url=base_url, api_key="k")
    return EmbeddingClient(mc)


def test_index_build_and_search_ranks_relevant_file(tmp_path, embeddings_server):
    (tmp_path / "auth.py").write_text("def login(user, password):\n    return authenticate(user)\n")
    (tmp_path / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    with embeddings_server() as srv:
        client = _embed_client(srv.base_url)
        index = CodeIndex(str(tmp_path))
        n = index.build(client)
        assert n >= 2
        assert index.path.is_file()

        results = index.search(client, "user login authenticate password", k=2)
        assert results
        assert results[0]["path"] == "auth.py"


def test_semantic_search_tool_without_index(tmp_path):
    cfg = Config()
    cfg.add_model(ModelConfig(name="m", model="x", base_url="http://x/v1", api_key="k"), True)
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True), config=cfg)
    res = build_registry().get("semantic_search").run({"query": "anything"}, ctx)
    assert res.is_error and "coded index" in res.content


def test_semantic_search_tool_end_to_end(tmp_path, embeddings_server):
    (tmp_path / "cache.py").write_text("class LRUCache:\n    def evict(self):\n        pass\n")
    with embeddings_server() as srv:
        # Build the index first.
        _embed_client(srv.base_url)
        cfg = Config()
        cfg.add_model(ModelConfig(name="m", model="x", provider="openai-compatible",
                                  base_url=srv.base_url, api_key="k"), True)
        cfg.embedding = {"provider": "openai-compatible", "model": "fake-embed",
                         "base_url": srv.base_url, "api_key": "k"}
        client = EmbeddingClient(resolve_embedding_model(cfg))
        CodeIndex(str(tmp_path)).build(client)

        ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True), config=cfg)
        res = build_registry().get("semantic_search").run({"query": "cache eviction LRU"}, ctx)
        assert not res.is_error
        assert "cache.py" in res.content


def test_web_fetch_strips_html(tmp_path, route_server):
    def route(method, path, body):
        html = "<html><head><style>x{}</style></head><body><h1>Title</h1><p>Hello world</p></body></html>"
        return 200, "text/html", html

    with route_server(route) as srv:
        ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True), config=None)
        res = build_registry().get("web_fetch").run({"url": srv.base + "/page"}, ctx)
        assert not res.is_error
        assert "Title" in res.content and "Hello world" in res.content
        assert "<h1>" not in res.content


def test_web_search_searxng_backend(tmp_path, route_server, monkeypatch):
    def route(method, path, body):
        if path.startswith("/search"):
            data = {"results": [{"title": "R1", "url": "http://a", "content": "snippet one"}]}
            return 200, "application/json", json.dumps(data)
        return 404, "application/json", "{}"

    with route_server(route) as srv:
        monkeypatch.setenv("SEARXNG_URL", srv.base)
        monkeypatch.setenv("CODED_SEARCH_BACKEND", "searxng")
        ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True), config=None)
        res = build_registry().get("web_search").run({"query": "test"}, ctx)
        assert not res.is_error
        assert "R1" in res.content and "http://a" in res.content


def test_image_encoding_and_multimodal_message(tmp_path):
    from coded.images import encode_image_data_url
    from coded.session import Session

    # Minimal 1x1 PNG.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
    )
    img = tmp_path / "pixel.png"
    img.write_bytes(png)
    url = encode_image_data_url(str(img))
    assert url.startswith("data:image/png;base64,")

    s = Session(system_prompt="sys")
    s.add_user_multimodal("what is this?", [str(img)])
    content = s.messages[-1]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
