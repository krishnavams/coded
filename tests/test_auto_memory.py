"""Auto project memory: fix notes, search, grouped recall, and the offer to
record a fix after the checks failed and were fixed."""

from __future__ import annotations

import json

from coded import trust
from coded.memory import MemoryStore
from coded.verify import _PYTEST
from tests.test_self_heal import _U, _agent, _heal_project, _write

from coded.permissions import PermissionManager


def test_fix_kind_search_and_grouped_recall(tmp_path):
    store = MemoryStore.for_project(str(tmp_path))
    store.add("Tests need TZ=UTC or the date tests fail.", "fix")
    store.add("Use tabs only in the Makefile.", "convention")
    store.add("Chose SQLite over Postgres for the CLI cache.", "decision")
    store.add("The CI runner has no network.", "fact")
    store.add("cli.py wires config into agent.py; tools never import the REPL.", "architecture")
    hits = store.search("date tests")
    assert hits and hits[0].kind == "fix"
    assert [e.kind for e in store.search("fix")] == ["fix"]
    assert store.search("") == []
    text = store.render(4000)
    order = [k for k in ("architecture", "convention", "decision", "fix", "fact") if f"({k})" in text]
    assert order == ["architecture", "convention", "decision", "fix", "fact"]
    assert [text.index(f"({k})") for k in order] == sorted(text.index(f"({k})") for k in order)


def test_old_notes_without_a_kind_still_load(tmp_path):
    (tmp_path / ".coded").mkdir()
    (tmp_path / ".coded" / "memory.json").write_text(json.dumps(
        {"entries": [{"id": "m1", "text": "An old note from before kinds."}]}))
    entries = MemoryStore.for_project(str(tmp_path)).load()
    assert entries[0].kind == "fact"


def test_memory_tool_search(tmp_path):
    from coded.config import Config
    from coded.tools.base import ToolContext
    from coded.tools.memory import MemoryTool

    MemoryStore.for_project(str(tmp_path)).add("Run migrations before the API tests.", "gotcha")
    ctx = ToolContext(cwd=str(tmp_path), permissions=PermissionManager(), config=Config())
    assert "migrations" in MemoryTool().run({"action": "search", "query": "api tests"}, ctx).content
    assert "No project notes" in MemoryTool().run({"action": "search", "query": "deploy"}, ctx).content


def _healing_script(nudges, remember=True):
    def script(body):
        last = body["messages"][-1]
        content = str(last.get("content"))
        if last["role"] == "user" and content.startswith("[memory]"):
            nudges.append(content)
            if not remember:
                return {"role": "assistant", "content": "Done."}, _U
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "mem", "type": "function", "function": {"name": "memory", "arguments":
                 json.dumps({"action": "remember", "kind": "fix", "text":
                             "add() subtracted: test_add caught it, use a + b."})}}]}, _U
        if last["role"] == "user" and "[verify]" in content:
            return _write("calc.py", "def add(a, b):\n    return a + b\n", "fix"), _U
        if last["role"] == "tool" and last.get("tool_call_id") == "mem":
            return {"role": "assistant", "content": "Fixed, and noted for next time."}, _U
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "Done."}, _U
        return _write("calc.py", "def add(a, b):\n    return a - b\n", "bug"), _U
    return script


def test_a_healed_turn_offers_one_fix_note(tmp_path, fake_server):
    _heal_project(tmp_path)
    trust.approve_check(str(tmp_path), _PYTEST)
    nudges = []
    with fake_server(_healing_script(nudges)) as server:
        agent = _agent(tmp_path, server.base_url, PermissionManager(auto_approve=True),
                       {"auto": True})
        answer = agent.run("add an add() function")
    assert len(nudges) == 1 and answer == "Fixed, and noted for next time."
    notes = MemoryStore.for_project(str(tmp_path)).load()
    assert [(n.kind, n.source) for n in notes] == [("fix", "agent")]


def test_no_offer_when_nothing_failed(tmp_path, fake_server):
    _heal_project(tmp_path)
    trust.approve_check(str(tmp_path), _PYTEST)
    nudges = []

    def script(body):
        last = body["messages"][-1]
        if last["role"] == "user" and str(last["content"]).startswith("[memory]"):
            nudges.append(1)
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "Done."}, _U
        return _write("calc.py", "def add(a, b):\n    return a + b\n", "ok"), _U

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url, PermissionManager(auto_approve=True),
                       {"auto": True})
        agent.run("add it")
    assert nudges == []
