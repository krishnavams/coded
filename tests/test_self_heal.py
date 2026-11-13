"""Self-healing: detected checks run after a change, failures go back to the
agent, the affected tests run first, and a repeated failure stops the loop."""

from __future__ import annotations

import json

from coded import trust
from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.verify import _PYTEST, detect_checks, narrow_checks, same_failure

_U = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def test_detects_the_projects_test_runner(tmp_path):
    assert detect_checks(str(tmp_path)) is None
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "test": 'echo "Error: no test specified" && exit 1'}}))
    assert detect_checks(str(tmp_path)) is None  # npm's placeholder is not a test suite
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert detect_checks(str(tmp_path)) == "npm test --silent"
    py = tmp_path / "py"
    (py / "tests").mkdir(parents=True)
    (py / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect_checks(str(py)) == _PYTEST
    (tmp_path / "go").mkdir()
    (tmp_path / "go" / "go.mod").write_text("module x\n")
    assert detect_checks(str(tmp_path / "go")) == "go test ./..."


def test_affected_tests_run_first(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "other.py").write_text("")
    (tmp_path / "tests" / "test_calc.py").write_text("from calc import add\n")
    (tmp_path / "tests" / "test_other.py").write_text("import other\n")
    assert narrow_checks(_PYTEST, str(tmp_path), ["calc.py"]) == _PYTEST + " tests/test_calc.py"
    assert narrow_checks("make test", str(tmp_path), ["calc.py"]) == "make test"
    assert narrow_checks(_PYTEST, str(tmp_path), []) == _PYTEST


def test_same_failure_ignores_timings_and_addresses():
    a = "FAILED tests/t.py::x - assert 1 == 2\n1 failed in 0.12s at 0x7f00"
    b = "FAILED tests/t.py::x - assert 1 == 2\n1 failed in 0.45s at 0x7f99"
    assert same_failure(a, b) and not same_failure("", b)
    assert not same_failure(a, "FAILED tests/t.py::y - other")


def _heal_project(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='calc'\nversion='0'\n")
    (tmp_path / "tests" / "test_calc.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")


def _agent(tmp_path, base_url, perms, verify):
    cfg = Config()
    m = ModelConfig(name="fake", model="fake", base_url=base_url, api_key="k")
    cfg.add_model(m, make_default=True)
    cfg.verify = verify
    return create_agent(model=m, config=cfg, cwd=str(tmp_path), permissions=perms,
                        stream=False, verbose=False)


def _write(path, content, cid):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
            "function": {"name": "write", "arguments": json.dumps(
                {"path": path, "content": content})}}]}


def test_a_failing_change_is_sent_back_and_fixed(tmp_path, fake_server):
    _heal_project(tmp_path)
    trust.approve_check(str(tmp_path), _PYTEST)
    sent_back = []

    def script(body):
        last = body["messages"][-1]
        if last["role"] == "user" and "[verify]" in str(last["content"]):
            sent_back.append(last["content"])
            return _write("calc.py", "def add(a, b):\n    return a + b\n", "fix"), _U
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "Done."}, _U
        return _write("calc.py", "def add(a, b):\n    return a - b\n", "bug"), _U

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url, PermissionManager(auto_approve=True),
                       {"auto": True})
        agent.run("add an add() function")
    assert len(sent_back) == 1 and "assert" in sent_back[0]
    assert "a + b" in (tmp_path / "calc.py").read_text()


def test_unapproved_checks_never_run_without_someone_to_ask(tmp_path, fake_server):
    _heal_project(tmp_path)
    calls = []

    def script(body):
        calls.append(body)
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "Done."}, _U
        return _write("calc.py", "def add(a, b):\n    return a - b\n", "bug"), _U

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url, PermissionManager(auto_approve=True),
                       {"auto": True})
        agent.run("add it")
    assert len(calls) == 2  # no [verify] round: the repo's code was not run unasked


def test_asks_once_then_remembers(tmp_path):
    _heal_project(tmp_path)
    asked = []
    perms = PermissionManager()
    perms.set_prompt(lambda title, detail: asked.append(detail) or "yes")
    agent = _agent(tmp_path, "http://x/v1", perms, {"auto": True})
    assert agent.auto_check_command(ask=True) == _PYTEST and _PYTEST in asked[0]
    perms.set_prompt(lambda t, d: asked.append(d) or "no")
    assert agent.auto_check_command(ask=True) == _PYTEST and len(asked) == 1


def test_the_same_failure_twice_stops_early(tmp_path, fake_server):
    _heal_project(tmp_path)
    trust.approve_check(str(tmp_path), _PYTEST)
    rounds = []

    def script(body):
        last = body["messages"][-1]
        if last["role"] == "user" and "[verify]" in str(last["content"]):
            rounds.append(1)
            return _write("calc.py", "def add(a, b):\n    return a - b\n", f"r{len(rounds)}"), _U
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "Done."}, _U
        return _write("calc.py", "def add(a, b):\n    return a - b\n", "bug"), _U

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url, PermissionManager(auto_approve=True),
                       {"auto": True, "max_rounds": 5})
        notes = []
        agent._status_note = notes.append
        agent.run("add it")
    assert len(rounds) == 1  # sent back once; the identical failure ended the loop
    assert any("failed the same way" in n for n in notes)


def test_a_narrowed_pass_is_confirmed_by_the_full_suite(tmp_path, fake_server):
    """A test that reaches the changed code only through a conftest fixture is
    invisible to the import graph; the full run must still catch it."""
    _heal_project(tmp_path)
    (tmp_path / "tests" / "conftest.py").write_text(
        "import sys, pathlib, pytest\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
        "@pytest.fixture\ndef doubled():\n    import calc\n    return calc.double(2)\n")
    (tmp_path / "tests" / "test_fixture.py").write_text(
        "def test_double(doubled):\n    assert doubled == 4\n")
    trust.approve_check(str(tmp_path), _PYTEST)
    sent_back = []

    def script(body):
        last = body["messages"][-1]
        if last["role"] == "user" and "[verify]" in str(last["content"]):
            sent_back.append(last["content"])
            return _write("calc.py", "def add(a, b):\n    return a + b\n\n"
                          "def double(x):\n    return x * 2\n", "fix"), _U
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "Done."}, _U
        return _write("calc.py", "def add(a, b):\n    return a + b\n\n"
                      "def double(x):\n    return x + 2 + 1\n", "bug"), _U

    with fake_server(script) as server:
        agent = _agent(tmp_path, server.base_url, PermissionManager(auto_approve=True),
                       {"auto": True})
        agent.run("add double()")
    assert len(sent_back) == 1 and "test_double" in sent_back[0]
