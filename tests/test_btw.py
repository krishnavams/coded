"""/btw: a side question answered from the conversation, without disturbing it."""

from __future__ import annotations

import threading
import time

import pytest

from coded import btw
from tests.test_agent_e2e import _make_agent

USAGE = {"prompt_tokens": 7, "completion_tokens": 3}


def _is_btw(body) -> bool:
    return btw.BTW_INSTRUCTION in str(body["messages"][-1].get("content", ""))


def test_side_messages_copy_the_conversation_and_drop_a_step_in_flight():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]}]
    out = btw.side_messages(msgs, "which file?")
    assert [m["role"] for m in out] == ["system", "user", "user"]
    assert out[-1]["content"].endswith("which file?")
    assert len(msgs) == 3  # the original is untouched


def test_btw_answers_without_touching_the_session(tmp_path, fake_server):
    seen = []

    def script(body):
        seen.append(body)
        return {"role": "assistant", "content": "It is in auth.py."}, USAGE

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        agent.session.add_user("we are fixing login")
        before = [dict(m) for m in agent.session.messages]
        assert btw.ask(agent, "where is the login code?") == "It is in auth.py."
    assert agent.session.messages == before                  # nothing added
    assert agent.session.total_usage.prompt_tokens == 7      # but it is paid for
    sent = seen[0]["messages"]
    assert "we are fixing login" in str(sent) and _is_btw(seen[0])
    assert btw.ask(agent, "  ").startswith("Usage: /btw")


def test_btw_is_answered_while_a_turn_is_running(tmp_path, fake_server):
    release = threading.Event()

    def script(body):
        if _is_btw(body):
            return {"role": "assistant", "content": "side answer"}, USAGE
        release.wait(10)  # the main turn is slow
        return {"role": "assistant", "content": "main answer"}, USAGE

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        result = {}
        turn = threading.Thread(target=lambda: result.setdefault("a", agent.run("long job")))
        turn.start()
        time.sleep(0.3)
        assert btw.ask(agent, "quick one?") == "side answer"
        assert turn.is_alive()                 # the turn was not interrupted
        release.set()
        turn.join(10)
    assert result["a"] == "main answer"
    assert not any("quick one?" in str(m.get("content")) for m in agent.session.messages)


def test_btw_typed_mid_turn_is_answered_not_queued(tmp_path, fake_server, monkeypatch):
    from rich.console import Console

    from coded import ui
    from coded.repl import Repl
    from coded.typeahead import TypeAhead
    from tests.test_progress_and_typeahead import _repl_config

    con = Console(width=100, record=True, color_system=None)
    monkeypatch.setattr(ui, "console", con)

    def script(body):
        return {"role": "assistant", "content": "the side answer"}, USAGE

    with fake_server(script) as server:
        agent = _make_agent(tmp_path, server.base_url)
        repl = Repl(agent, _repl_config(tmp_path))
        ta = TypeAhead(poll=lambda _t: None, intercept=repl._intercept_midturn)
        ta.feed("/btw what does auth.py do?")
        ta.feed("then add a test")
        deadline = time.time() + 10
        while "the side answer" not in con.export_text(clear=False) and time.time() < deadline:
            time.sleep(0.05)
    out = con.export_text()
    assert "the side answer" in out and "not added to the conversation" in out
    assert ta.queued_items == ["then add a test"]   # only the real follow-up waits


@pytest.mark.asyncio
async def test_tui_btw_mid_turn(tmp_path, fake_server):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from coded.tui import CodedTUI

    def script(body):
        return {"role": "assistant", "content": "tui side answer"}, USAGE

    with fake_server(script) as server:
        app = CodedTUI(_make_agent(tmp_path, server.base_url))
        async with app.run_test(size=(120, 40)) as pilot:
            app.busy = True                     # as if a turn were running
            app.query_one("#prompt", Input).value = "/btw what is x?"
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.1)
                if app.query(".btw"):
                    break
            from rich.console import Console

            con = Console(width=100, record=True, color_system=None)
            panel = app.query_one(".btw")
            con.print(getattr(panel, "renderable", None) or panel.content)
            text = con.export_text()
            assert "tui side answer" in text and "what is x?" in text
            assert app._queued == []
            app.busy = False
