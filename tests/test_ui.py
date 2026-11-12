"""Smoke tests for the terminal UI helpers (they must not raise)."""

from __future__ import annotations

import pytest

from coded import ui
from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.permissions import PermissionManager
from coded.repl import Repl


def test_ui_helpers_render_without_error():
    ui.banner("gpt-4o", "/some/very/long/path/" + "x" * 80)
    ui.echo_user("hello [not-markup] world")
    ui.tool_call("edit", {"path": "a.py", "old_string": "x"})
    ui.tool_result("line1\nline2", is_error=False)
    ui.tool_result("boom", is_error=True)
    ui.permission_panel("Run shell command", "rm -rf /tmp/x")
    ui.diff("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new")
    for fn in (ui.info, ui.warn, ui.error, ui.success):
        fn("message")


def test_thinking_spinner_is_context_manager():
    with ui.thinking("Working"):
        pass
    ui.set_quiet(True)
    try:
        s = ui.thinking()
        s.start()
        s.stop()
    finally:
        ui.set_quiet(False)


def test_repl_bottom_toolbar(headless):
    from prompt_toolkit.formatted_text import to_formatted_text

    cfg = Config()
    m = ModelConfig(name="gpt-4o", model="gpt-4o", base_url="http://x/v1", api_key="k",
                    input_cost=2.5, output_cost=10.0)
    cfg.add_model(m, make_default=True)
    agent = create_agent(model=m, config=cfg, cwd=".",
                         permissions=PermissionManager(), stream=False, verbose=False)
    agent.session.turns = 2
    agent.session.total_usage.prompt_tokens = 1500
    agent.session.total_usage.completion_tokens = 500
    repl = Repl(agent, cfg)
    text = "".join(t[1] for t in to_formatted_text(repl._bottom_toolbar()))
    assert "gpt-4o" in text
    assert "2 turns" in text
    assert "2.0k" in text  # 2000 tokens


def test_repl_input_is_boxed_with_the_status_line_right_under_it(headless):
    from prompt_toolkit.layout import FloatContainer, HSplit

    cfg = Config()
    m = ModelConfig(name="gpt-4o", model="gpt-4o", base_url="http://x/v1", api_key="k")
    cfg.add_model(m, make_default=True)
    agent = create_agent(model=m, config=cfg, cwd=".",
                         permissions=PermissionManager(), stream=False, verbose=False)
    repl = Repl(agent, cfg)
    assert repl._boxed
    root = repl.prompt.app.layout.container
    assert isinstance(root, FloatContainer) and root.floats  # menus open over the filler
    rows = root.content.children
    assert isinstance(rows[0], type(rows[2]))  # top edge, input, bottom edge
    status = rows[3].content.content.text   # the status line comes straight after
    assert status == repl._bottom_toolbar
    assert repl.prompt.bottom_toolbar is None  # nothing pinned to the terminal's last row
    assert repl.prompt.reserve_space_for_menu == 0


def test_repl_shows_the_tasks_right_above_the_prompt(headless, capsys):
    cfg = Config()
    m = ModelConfig(name="gpt-4o", model="gpt-4o", base_url="http://x/v1", api_key="k")
    cfg.add_model(m, make_default=True)
    agent = create_agent(model=m, config=cfg, cwd=".",
                         permissions=PermissionManager(), stream=False, verbose=False)
    repl = Repl(agent, cfg)
    assert not repl._show_tasks_above_prompt()  # no tasks: nothing between rule and prompt

    agent.todos.items = [{"content": "Write it", "status": "completed"},
                         {"content": "Test it", "status": "in_progress"}]
    capsys.readouterr()
    assert repl._show_tasks_above_prompt()
    out = capsys.readouterr().out
    assert "Test it" in out and "1/2" in out
    assert repl._show_tasks_above_prompt()  # unfinished: shown before every prompt

    agent.todos.items = [{"content": "Write it", "status": "completed"},
                         {"content": "Test it", "status": "completed"}]
    assert repl._show_tasks_above_prompt()  # the finished list, once...
    assert not repl._show_tasks_above_prompt()  # ...then not again


def test_tool_titles_are_readable_and_relative():
    assert ui.tool_title("edit", {"path": "/proj/src/app.py", "old_string": "x"}, "/proj") == \
        ("Edit", "src/app.py")
    assert ui.tool_title("bash", {"command": "pytest -q\n  tests"}) == ("Bash", "pytest -q tests")
    assert ui.tool_title("todo_write", {"todos": []}) == ("Update tasks", "")
    label, summary = ui.tool_title("mcp__db__query", {"sql": "select 1", "limit": 5})
    assert label == "mcp__db__query" and "sql: select 1" in summary and "limit: 5" in summary
    assert ui.tool_call_text("read", {"path": "a.py"}).plain == "● Read(a.py)"


def test_tool_result_display_is_short_and_project_relative():
    out = ui.tool_result_text("1\tx\n2\ty\n3\tz", name="read").plain
    assert out.strip() == "⎿  Read 3 lines"
    out = ui.tool_result_text("Edited /proj/app.py (1 replacement(s)).", cwd="/proj").plain
    assert "Edited app.py" in out and "/proj" not in out
    long = ui.tool_result_text("\n".join(map(str, range(40)))).plain
    assert "… +28 lines" in long


def test_diff_display_has_line_numbers_and_no_file_headers():
    d = "--- a/app.py\n+++ b/app.py\n@@ -3,3 +3,3 @@\n for i in items:\n-    s += 1\n+    s += 2\n return s\n"
    out = ui.diff_text(d).plain
    assert "a/app.py" not in out and "@@" not in out
    assert "   4 -     s += 1" in out and "   4 +     s += 2" in out and "   5   return s" in out


def test_logo_is_coded_own_mark():
    assert len(ui.LOGO) == 3 and len({len(r) for r in ui.LOGO}) == 1
    assert "◆" in ui.logo_text().plain


def test_repl_shift_tab_is_bound_to_plan_mode(headless):
    from prompt_toolkit.formatted_text import to_formatted_text

    cfg = Config()
    m = ModelConfig(name="m", model="m", base_url="http://x/v1", api_key="k")
    cfg.add_model(m, make_default=True)
    agent = create_agent(model=m, config=cfg, cwd=".", permissions=PermissionManager(),
                         stream=False, verbose=False)
    repl = Repl(agent, cfg)
    binding = [b for b in repl.prompt.key_bindings.bindings
               if [getattr(k, "value", k) for k in b.keys] == ["s-tab"]]
    assert len(binding) == 1

    class _Event:
        app = type("A", (), {"invalidate": lambda self: None})()

    binding[0].handler(_Event())
    assert agent.permission_mode == "accept-edits"
    assert "accept edits" in "".join(t[1] for t in to_formatted_text(repl._bottom_toolbar()))
    binding[0].handler(_Event())
    assert agent.mode == "plan"
    assert "plan mode" in "".join(t[1] for t in to_formatted_text(repl._bottom_toolbar()))
    binding[0].handler(_Event())
    assert agent.mode == "default"
