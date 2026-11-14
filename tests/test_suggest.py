"""The suggestion engine: matching, grammar, live values, learning, context."""

from __future__ import annotations

import json
import time

import pytest

from coded.events import EventLog
from coded.repl import SLASH_COMMANDS
from coded.suggest import (Context, Engine, MenuState, UsageStore, fuzzy, next_steps,
                           quote_if_needed, tokenize)

BUILTIN = {k: (v, "built-in") for k, v in SLASH_COMMANDS.items()}


def _engine(tmp_path, **kw):
    ctx = Context(cwd=str(tmp_path), commands=dict(BUILTIN), **kw)
    return Engine(ctx, UsageStore(str(tmp_path), tmp_path / "usage.json"))


def _shown(engine, text, cursor=None):
    return [s.display for s in engine.suggest(text, cursor)]


# -- matching -----------------------------------------------------------------------

@pytest.mark.parametrize("query,cand,positions", [
    ("/rev", "/review", (0, 1, 2, 3)),          # prefix
    ("view", "/review", (3, 4, 5, 6)),          # substring
    ("/rvw", "/review", (0, 1, 3, 6)),          # letters in order
    ("/ad", "/add-dir", (0, 1, 2)),
])
def test_fuzzy_finds_and_marks_the_match(query, cand, positions):
    score, pos = fuzzy(query, cand)
    assert pos == positions and score > 0


def test_fuzzy_ranks_prefix_over_substring_over_letters_over_typos():
    prefix = fuzzy("/rev", "/review")[0]
    sub = fuzzy("view", "/review")[0]
    letters = fuzzy("/rvw", "/review")[0]
    typo = fuzzy("/reveiw", "/review")[0]
    assert prefix > sub > letters > typo > 0
    assert fuzzy("/xyz", "/review") is None


def test_typos_are_tolerated_and_corrected(tmp_path):
    e = _engine(tmp_path)
    assert _shown(e, "/reveiw")[0] == "/review"       # transposed letters
    assert _shown(e, "/permisions")[0] == "/permissions"
    assert e.correct("/reviw") == ["/review"]
    assert "/help" in e.correct("/hlep")
    assert e.correct("/qqqqqq") == []


# -- commands, subcommands, flags, values ---------------------------------------------

def test_a_bare_slash_lists_commands_in_their_own_order(tmp_path):
    assert _shown(_engine(tmp_path), "/")[:3] == ["/help", "/model", "/models"]


def test_rows_carry_description_syntax_and_source(tmp_path):
    from coded.commands import CustomCommand as Command

    custom = {"deploy": Command(name="deploy", description="Ship it", body="x",
                                path=tmp_path / "d.md", argument_hint="[env]")}
    e = _engine(tmp_path, custom=custom)
    top = e.suggest("/mod")[0]
    assert (top.display, top.kind, top.source) == ("/model", "command", "built-in")
    assert top.usage == "/model [name]" and "model" in top.description
    dep = e.suggest("/depl")[0]
    assert (dep.source, dep.usage) == ("custom command", "/deploy [env]")


def test_subcommands_and_their_arguments_follow_the_grammar(tmp_path):
    e = _engine(tmp_path)
    assert _shown(e, "/permissions ") == ["allow", "deny", "remove", "mode", "save"]
    assert _shown(e, "/permissions mode ") == ["default", "accept-edits", "plan"]
    assert _shown(e, "/memory ")[:5] == ["list", "search", "add", "forget", "clear"]
    assert _shown(e, "/route ") == ["on", "off"]
    assert _shown(e, "/permissions save ") == []           # takes nothing more
    assert _shown(e, "/btw what is ") == []                 # free text: no noise


def test_flags_are_suggested_once(tmp_path):
    e = _engine(tmp_path)
    assert set(_shown(e, "/mission --")) == {"--budget", "--resume", "--list"}
    assert "--resume" not in _shown(e, "/mission --resume --")
    assert _shown(e, "/rewind --k") == ["--keep-files"]
    assert _shown(e, "/mission --budget ") == []            # its value is a number


def test_live_values_models_styles_rules_turns(tmp_path, fake_server):
    from coded.config import Config, ModelConfig
    from tests.test_agent_e2e import _make_agent

    with fake_server(lambda b: ({"role": "assistant", "content": "ok"}, None)) as server:
        agent = _make_agent(tmp_path, server.base_url)
        cfg = Config()
        cfg.add_model(ModelConfig(name="fast", model="small-1", base_url="http://x/v1"))
        cfg.add_model(ModelConfig(name="deep", model="big-2", base_url="http://x/v1"))
        agent.run("first request [x]")
        agent.run("second request")
        e = _engine(tmp_path, agent=agent, config=cfg)
        assert {"fast", "deep"} <= set(_shown(e, "/model "))
        assert "concise" in _shown(e, "/style ")
        assert _shown(e, "/rewind ")[:2] == ["2", "1"]          # newest request first
        assert "first request [x]" in e.suggest("/rewind 1")[0].description
        assert _shown(e, "/permissions allow ba")[:2] == ["bash", "bash(*)"]
        assert "edit(*)" in _shown(e, "/permissions allow edi")
        agent.permissions.add_rule("deny", "bash(rm *)")
        e.invalidate()
        assert _shown(e, "/permissions remove ") == ["bash(rm *)"]


def test_rule_suggestions_learn_the_shell_commands_the_agent_ran(tmp_path, fake_server):
    from tests.test_agent_e2e import _make_agent

    calls = iter([{"role": "assistant", "content": "", "tool_calls": [{
        "id": "c1", "type": "function", "function": {
            "name": "bash", "arguments": json.dumps({"command": "pytest -q tests"})}}]},
        {"role": "assistant", "content": "done"}])
    with fake_server(lambda b: (next(calls), None)) as server:
        agent = _make_agent(tmp_path, server.base_url)
        agent.run("run the tests")
        e = _engine(tmp_path, agent=agent)
        assert _shown(e, "/permissions allow ")[0] == "bash(pytest *)"


def test_paths_folders_mentions_and_quoting(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x")
    (tmp_path / "my notes.md").write_text("x")
    (tmp_path / "shot.png").write_bytes(b"x")
    e = _engine(tmp_path)
    assert _shown(e, "/review sr")[:2] == ["src/", "src/app.py"]
    assert e.suggest("/review sr")[0].text == "src/"            # a folder: no space, go on
    assert _shown(e, "/add-dir ") == ["src/"]                  # folders only
    assert set(_shown(e, "/image ")) == {"src/", "shot.png"}    # images (and folders)
    notes = next(s for s in e.suggest("/export my") if s.display == "my notes.md")
    assert notes.text == '"my notes.md" '                       # spaces get quoted
    mention = e.suggest("look at @app")[0]
    assert (mention.text, mention.start) == ("@src/app.py", 8)
    assert e.suggest('/export "my n')[0].text == '"my notes.md" '


def test_multiline_messages_complete_commands_only_on_the_first_line(tmp_path):
    (tmp_path / "app.py").write_text("x")
    e = _engine(tmp_path)
    text = "explain this\n/rev"
    assert e.suggest(text) == []                    # a command must start the message
    text = "/review\nand see @ap"
    s = e.suggest(text)[0]
    assert s.display == "app.py" and s.apply(text) == ("/review\nand see @app.py", 23)


def test_the_cursor_decides_which_word_is_completed(tmp_path):
    e = _engine(tmp_path)
    text = "/permissions mo plan"
    s = e.suggest(text, cursor=15)[0]               # cursor after "mo"
    assert s.display == "mode" and s.apply(text)[0] == "/permissions mode  plan"
    assert tokenize('/x "a b" c\\ d')[1].value == "a b"
    assert tokenize('/x "a b" c\\ d')[2].value == "c d"
    assert quote_if_needed("plain") == "plain" and quote_if_needed('a "b"') == '"a \\"b\\""'


# -- learning ---------------------------------------------------------------------------

def test_used_commands_rank_higher_and_whole_lines_come_back(tmp_path):
    e = _engine(tmp_path)
    before = _shown(e, "/re")
    assert before.index("/resume") > 0
    for _ in range(4):
        e.record("/resume 2")
    e2 = _engine(tmp_path)                           # read back from disk
    after = _shown(e2, "/re")
    assert after[0] == "/resume"
    assert "/resume 2" in after                      # the whole line, as used
    assert next(s for s in e2.suggest("/re") if s.display == "/resume 2").kind == "recent"
    e2.record("/not-a-command")
    assert "/not-a-command" not in UsageStore(str(tmp_path), tmp_path / "usage.json").lines()


def test_usage_is_per_project_and_bounded(tmp_path):
    path = tmp_path / "usage.json"
    a, b = UsageStore(str(tmp_path / "a"), path), UsageStore(str(tmp_path / "b"), path)
    a.record("/review")
    assert a.boost("/review") > 0 and b.boost("/review") == 0
    for i in range(UsageStore.MAX_LINES + 30):
        a.record(f"/model m{i}")
    assert len(json.loads(path.read_text())[a.key]) == UsageStore.MAX_LINES


# -- context ----------------------------------------------------------------------------

class _Agent:
    """Just the state next_steps reads."""

    def __init__(self, events=(), active=(), running=(), todos=()):
        self.events = EventLog(list(events))
        self.active_tools = list(active)
        self.tasks = type("T", (), {"running": lambda s: list(running), "all": lambda s: []})()
        self.shells = type("S", (), {"list": lambda s: []})()
        self.todos = type("D", (), {"items": list(todos)})()
        self.permission_mode = "default"
        self.self_heal = False
        self.session = type("Sess", (), {"turns": 1, "messages": [], "last_prompt_tokens": 0})()
        self.model = None


def _ctx(tmp_path, agent):
    return Context(cwd=str(tmp_path), commands=dict(BUILTIN), agent=agent)


def test_next_steps_follow_what_just_happened(tmp_path):
    turn = {"kind": "turn", "prompt": "x"}
    failed = _Agent([turn, {"kind": "tool", "name": "edit", "error": False},
                     {"kind": "verify", "result": "fail"}])
    steps = dict(next_steps(_ctx(tmp_path, failed)))
    assert "/heal" in steps and "checks failed" in steps["/heal"]
    assert "/review" in steps and "/undo" in steps
    denied = _Agent([turn, {"kind": "tool", "name": "bash", "error": True, "denied": True,
                            "reason": "user"}])
    assert "/permissions" in dict(next_steps(_ctx(tmp_path, denied)))
    old = _Agent([turn, {"kind": "verify", "result": "fail"}, turn])   # an earlier request
    assert "/heal" not in dict(next_steps(_ctx(tmp_path, old)))


def test_while_a_tool_runs_the_running_work_is_suggested_first(tmp_path):
    busy = _Agent(active=["task"], running=[object(), object()])
    e = Engine(_ctx(tmp_path, busy), UsageStore(str(tmp_path), tmp_path / "u.json"))
    top = e.suggest("/")[0]
    assert top.display == "/tasks" and top.kind == "next" and "2 sub-agents" in top.reason
    assert "/btw" in [s.display for s in e.suggest("/")[:4]]


def test_next_steps_only_name_commands_that_exist_here(tmp_path):
    busy = _Agent(active=["bash"])
    ctx = Context(cwd=str(tmp_path), commands={"/btw": ("", "built-in")}, agent=busy)
    assert [c for c, _ in next_steps(ctx)] == ["/btw"]


# -- reliability --------------------------------------------------------------------------

def test_a_failing_source_suggests_nothing_instead_of_raising(tmp_path, monkeypatch):
    import coded.suggest as suggest

    monkeypatch.setitem(suggest.PROVIDERS, "models", lambda ctx: 1 / 0)
    e = _engine(tmp_path)
    assert e.suggest("/model ") == []
    assert _shown(e, "/mod")[0] == "/model"                # the rest still works


def test_large_registries_answer_fast_and_cache(tmp_path):
    from coded.commands import CustomCommand as Command

    custom = {f"cmd{i:04d}": Command(name=f"cmd{i:04d}", description=f"command {i}", body="x",
                                     path=tmp_path / "c.md") for i in range(3000)}
    e = _engine(tmp_path, custom=custom)
    start = time.perf_counter()
    items = e.suggest("/cmd12")
    first = time.perf_counter() - start
    assert items[0].display.startswith("/cmd12") and len(items) <= 12
    start = time.perf_counter()
    e.suggest("/cmd12")
    again = time.perf_counter() - start
    assert first < 0.5 and again < 0.005                # cached for the same input


def test_duplicate_rows_are_dropped(tmp_path):
    e = _engine(tmp_path)
    e.record("/style concise")
    texts = [s.text for s in e.suggest("/sty")]
    assert len(texts) == len(set(t.rstrip() for t in texts))


def test_suggestions_never_run_anything(tmp_path):
    """Taking a row only edits text; it returns the new line and cursor."""
    e = _engine(tmp_path)
    s = e.suggest("/cle")[0]
    assert s.apply("/cle") == ("/clear ", 7)

    class _Editor:
        def __init__(self):
            self.text, self.cursor = "/cle", 4

        def snapshot(self):
            from coded.lineedit import Snapshot
            return Snapshot(self.text, self.cursor, None)

        def set_text(self, t, c=None):
            self.text, self.cursor = t, c

    ed = _Editor()
    menu = MenuState(e)
    assert menu.accept(ed, explicit=True) is None            # nothing picked: Enter sends
    menu.move(ed.text, ed.cursor, 1)
    assert menu.accept(ed, explicit=True).display == "/clear" and ed.text == "/clear "
    menu.dismiss("/cle")
    assert menu.items("/cle", 4) == [] and menu.items("/cl", 3)   # reopens on change
