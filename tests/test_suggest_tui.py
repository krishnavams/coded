"""The TUI's suggestion dropdown: shown while typing, keys only while open,
click to take, never submits on its own; unknown commands are not sent on."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from textual.widgets import Input, OptionList  # noqa: E402

from coded.tui import CodedTUI, StatusNote  # noqa: E402
from tests.test_tui import _agent  # noqa: E402


def _ok(body):
    return {"role": "assistant", "content": "hello"}, None


async def _type(pilot, text):
    for ch in text:
        await pilot.press(ch if ch != " " else "space")
    await pilot.pause()


@pytest.mark.asyncio
async def test_dropdown_keys_take_without_sending(tmp_path, fake_server):
    with fake_server(_ok) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)
            box = app.query_one("#suggest", OptionList)
            await _type(pilot, "/re")
            assert box.has_class("shown") and box.option_count >= 3
            await pilot.press("down", "down")
            await pilot.pause()
            assert box.highlighted == 1
            second = app._menu_items[1].text
            await pilot.press("enter")              # takes the row...
            await pilot.pause()
            assert prompt.value == second and not app.busy   # ...and sends nothing
            await pilot.press("escape")             # Esc closes it (if args reopened it)
            await pilot.pause()
            assert not box.has_class("shown")
            prompt.value = ""
            await _type(pilot, "/und")
            await pilot.press("tab")                # Tab takes the first row
            await pilot.pause()
            assert prompt.value == "/undo "
            assert app.focused is prompt            # ...and focus stays in the input


@pytest.mark.asyncio
async def test_closed_dropdown_leaves_tab_and_escape_alone(tmp_path, fake_server):
    with fake_server(_ok) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)
            await _type(pilot, "fix the bug")
            assert not app.query_one("#suggest", OptionList).has_class("shown")
            await pilot.press("tab")                # not a menu: Tab moves focus as before
            await pilot.pause()
            assert app.focused is not prompt


@pytest.mark.asyncio
async def test_clicking_a_row_takes_it(tmp_path, fake_server):
    with fake_server(_ok) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test() as pilot:
            prompt = app.query_one("#prompt", Input)
            await _type(pilot, "/tim")
            box = app.query_one("#suggest", OptionList)
            assert box.has_class("shown")
            await pilot.click("#suggest", offset=(4, 1))
            await pilot.pause()
            assert prompt.value == "/timeline " and app.focused is prompt and not app.busy


@pytest.mark.asyncio
async def test_unknown_commands_are_explained_not_sent_to_the_model(tmp_path, fake_server):
    seen = []

    def script(body):
        seen.append(body["messages"][-1]["content"])
        return _ok(body)

    with fake_server(script) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        async with app.run_test() as pilot:
            for line in ("/tmieline", "/model fast"):
                app._submit(line)
                await pilot.pause()
            notes = [str(w.render()) for w in app.query(StatusNote)]
            assert any("Did you mean /timeline" in n for n in notes)
            assert any("/model works in the REPL" in n for n in notes)
            app._submit("/etc/hosts: what is this file?")   # a path, not a command
            await app.workers.wait_for_complete()
            await pilot.pause()
    assert seen and seen[-1].startswith("/etc/hosts") and len(seen) == 1
