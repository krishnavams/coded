"""Smoke tests for the terminal UI helpers (they must not raise)."""

from __future__ import annotations

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


def test_repl_bottom_toolbar():
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
    assert "turns 2" in text
    assert "2.0k" in text  # 2000 tokens
