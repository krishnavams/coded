"""Choice prompts: permission ("Do you want to proceed?") and ask_user questions,
including several questions with a review page — the selector, the key reader,
the REPL dock and the TUI modal."""

from __future__ import annotations

import io
import json
import os
import threading
import time

import pytest

from coded import choice, ui
from coded.agent import create_agent
from coded.choice import Chooser, Question
from coded.config import Config, ModelConfig
from coded.lineedit import LineEditor
from coded.permissions import Decision, PermissionManager
from coded.tools.base import ToolContext
from coded.tools.interact import AskUserTool
from coded.typeahead import KEY, TypeAhead, _PosixKeys

_USAGE = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def _keys(chooser, *keys):
    for k in keys:
        chooser.key(k)
    return chooser


def _plain(chooser, **kw) -> str:
    from rich.console import Console

    # In memory: a real file would be opened in the platform encoding (cp1252
    # on Windows), which cannot hold the prompt's arrows.
    con = Console(width=100, record=True, file=io.StringIO())
    con.print(chooser.render(**kw))
    return con.export_text()


# -- the selector ---------------------------------------------------------------
def test_arrows_and_enter_pick_an_option():
    c = _keys(Chooser([Question.make("Which DB?", ["Postgres", "SQLite"])]), "down", "enter")
    assert c.done and [str(a) for a in c.result()] == ["SQLite"]


def test_a_digit_picks_directly_and_esc_cancels():
    assert str(_keys(Chooser([Question.make("Q?", ["a", "b", "c"])]), "3").result()[0]) == "c"
    c = _keys(Chooser([Question.make("Q?", ["a"])]), "escape")
    assert c.done and c.cancelled and c.result() is None


def test_type_something_row_takes_a_free_answer():
    c = Chooser([Question.make("Name?", ["foo"])])
    _keys(c, "down")
    assert c.editing
    _keys(c, "enter")  # empty: nothing happens
    assert not c.done
    _keys(c, *"bar 1", "backspace", "2", "enter")
    assert str(c.result()[0]) == "bar 2"
    c = _keys(Chooser([Question.make("Name?", ["foo"])]), "2")  # its number focuses it
    assert c.editing and not c.done


def test_several_questions_end_on_a_review_page_before_submitting():
    qs = [Question.make("Language?", ["Python", "Go"], header="Lang"),
          Question.make("Features?", [{"label": "Auth", "description": "login"},
                                      {"label": "Cache"}, {"label": "Logs"}],
                        header="Features", multi_select=True)]
    c = Chooser(qs)
    _keys(c, "2")                       # Go, moves to question 2
    assert c.tab == 1
    _keys(c, " ", "down", "down", " ")  # toggle Auth and Logs
    _keys(c, "down", "down", "enter")   # past "Type something." to "Next →"
    assert c.on_review and not c.done
    text = _plain(c)
    assert "Review your answers" in text and "Go" in text and "Auth, Logs" in text
    assert "Submit answers" in text and "☒ Lang" in text
    _keys(c, "enter")
    assert [str(a) for a in c.result()] == ["Go", "Auth, Logs"]


def test_review_can_go_back_or_cancel_and_warns_about_unanswered():
    c = Chooser([Question.make("A?", ["x"]), Question.make("B?", ["y"])])
    _keys(c, "tab", "tab")
    assert c.on_review and "not answered" in _plain(c)
    _keys(c, "left")
    assert c.tab == 1
    _keys(c, "1", "down", "enter")      # answer B, then Cancel on the review
    assert c.cancelled and c.result() is None


def test_line_mode_numbers_text_and_submit():
    c = Chooser([Question.make("A?", ["x", "y"]),
                 Question.make("B?", ["p", "q", "r"], multi_select=True)])
    c.line("2")
    c.line("1, 3")
    assert c.on_review
    c.line("")
    assert [str(a) for a in c.result()] == ["y", "p, r"]
    c = Chooser([Question.make("A?", ["x"])])
    c.line("something else")
    assert str(c.result()[0]) == "something else"
    assert "a number or your own answer" in _plain(Chooser([Question.make("A?", ["x"])]),
                                                  line_mode=True)


# -- permission prompt ------------------------------------------------------------
def test_permission_prompt_choices():
    def run(*keys):
        c = choice.permission_chooser("Run shell command", "rm -rf build")
        return choice.permission_answer(_keys(c, *keys))

    assert run("enter") == "yes"
    assert run("2") == "always"
    assert run("escape") == "no"
    assert run("down", "down", "enter") == "no"
    assert run("3") == "no"
    assert run("down", "down", *"use pytest -x", "enter") == "no: use pytest -x"
    text = _plain(choice.permission_chooser("Run shell command", "rm -rf build"))
    for want in ("Run shell command", "rm -rf build", "Do you want to proceed?", "1. Yes",
                 "don't ask again", "No, and tell coded what to do differently (esc)"):
        assert want in text


def test_a_no_with_feedback_reaches_the_model(tmp_path, fake_server):
    requests = []

    def script(body):
        requests.append(body)
        if any(m.get("role") == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "ok"}, _USAGE
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": "rm -rf build"})}}]}, _USAGE

    perms = PermissionManager()
    perms.set_prompt(lambda title, detail: "no: run make clean instead")
    with fake_server(script) as server:
        cfg = Config()
        m = ModelConfig(name="fake", model="fake", base_url=server.base_url, api_key="k")
        cfg.add_model(m, make_default=True)
        agent = create_agent(model=m, config=cfg, cwd=str(tmp_path), permissions=perms,
                             stream=False, verbose=False)
        agent.run("clean up")
    tool_msg = next(m for m in requests[-1]["messages"] if m["role"] == "tool")
    assert "run make clean instead" in tool_msg["content"]
    assert perms.take_feedback() == ""  # consumed once


def test_feedback_does_not_leak_into_a_later_denial():
    perms = PermissionManager(rules={"deny": ["bash(rm *)"]})
    perms.set_prompt(lambda t, d: "no: not like that")
    assert perms.request(key="edit", title="Edit", detail="a.py") is Decision.DENY
    assert perms.request(key="bash", title="Run", detail="rm x") is Decision.DENY
    assert perms.take_feedback() == ""


# -- ask_user with several questions ----------------------------------------------
def _ctx(tmp_path, perms):
    return ToolContext(cwd=str(tmp_path), permissions=perms, config=Config(), extra={})


def test_ask_user_several_questions(tmp_path):
    perms = PermissionManager()
    seen = []

    def front(questions):
        seen.extend(questions)
        return ["Go", ""]

    perms.set_questions(front)
    args = {"questions": [
        {"question": "Language?", "header": "Lang", "options": ["Python", "Go"]},
        {"question": "Features?", "options": [{"label": "Auth", "description": "login"}],
         "multi_select": True}]}
    out = AskUserTool().run(args, _ctx(tmp_path, perms)).content
    assert out.splitlines() == ["The user answered:", "- Language? → Go",
                                "- Features? → (skipped: use your best judgment)"]
    assert seen[0].header == "Lang" and seen[1].multi_select
    assert seen[1].options[0].description == "login"
    perms.set_questions(lambda qs: None)  # Esc / Cancel
    assert "declined" in AskUserTool().run(args, _ctx(tmp_path, perms)).content
    assert AskUserTool().run({"questions": [{"question": ""}]}, _ctx(tmp_path, perms)).is_error


def test_old_single_question_front_ends_still_get_each_question(tmp_path):
    perms = PermissionManager()
    perms.set_question(lambda q, opts: opts[-1])
    args = {"questions": [{"question": "A?", "options": ["x", "y"]},
                          {"question": "B?", "options": ["z"]}]}
    out = AskUserTool().run(args, _ctx(tmp_path, perms)).content
    assert "A? → y" in out and "B? → z" in out


def test_ask_user_title_for_several_questions():
    assert ui.tool_title("ask_user", {"questions": [{"question": "A?"}, {"question": "B?"}]}) \
        == ("Question", "2 questions")


# -- key reading ------------------------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="POSIX reader: select() on a pipe; "
                    "Windows reads keys through msvcrt instead")
def test_posix_reader_hands_over_single_keys_in_key_mode():
    r, w = os.pipe()
    keys = _PosixKeys(fd=r, out=io.StringIO())
    keys._saved = object()  # a pipe has no terminal modes to set
    keys.keys_mode = True
    os.write(w, b"\x1b[B\x1b[Ax\r\x7f\t")
    got = []
    for _ in range(8):
        k = keys(0.05)
        if k:
            got.append(k[len(KEY):])
    assert got == ["down", "up", "x", "enter", "backspace", "tab"]
    os.write(w, b"\x1b")
    assert keys(0.05) == KEY + "escape"  # a lone Esc cancels, it does not stop the turn
    keys._saved = None
    os.close(r)
    os.close(w)


class _KeyPoll:
    keys_mode = False

    def __init__(self):
        self.editor = LineEditor()

    def __call__(self, timeout):
        time.sleep(timeout)
        return None


def _repl(tmp_path):
    from coded.repl import Repl

    cfg = Config()
    m = ModelConfig(name="m", model="m", base_url="http://x/v1", api_key="k")
    cfg.add_model(m, make_default=True)
    agent = create_agent(model=m, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(), stream=False, verbose=False)
    return Repl(agent, cfg)


def _drive(ta, fn, keys):
    out = []
    t = threading.Thread(target=lambda: out.append(fn()))
    t.start()
    for _ in range(200):
        if ta._want_keys.is_set():
            break
        time.sleep(0.01)
    for k in keys:
        ta.push_key(k)
    t.join(timeout=5)
    return out


def test_repl_prompts_run_in_the_dock_with_keys(headless, tmp_path):
    repl = _repl(tmp_path)
    poll = _KeyPoll()
    with TypeAhead(poll=poll) as ta:
        repl._typeahead = ta
        dock = ui.TurnDock(lambda: "").start()
        try:
            assert _drive(ta, lambda: repl._permission_prompt("Run shell command", "ls"),
                          ["down", "down", *"use ls -la", "enter"]) == ["no: use ls -la"]
            assert not poll.keys_mode and dock._prompt is None  # restored
            questions = [Question.make("A?", ["x", "y"]), Question.make("B?", ["z"])]
            assert _drive(ta, lambda: repl._questions_prompt(questions),
                          ["down", "enter", "enter", "enter"]) == [["y", "z"]]
        finally:
            dock.stop()
            repl._typeahead = None


# -- TUI ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tui_several_questions_with_review(tmp_path, fake_server):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from coded.tui import CodedTUI, QuestionModal

    requests = []

    def script(body):
        requests.append(body)
        if any(m.get("role") == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": "Thanks."}, _USAGE
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "q1", "type": "function", "function": {"name": "ask_user", "arguments": json.dumps(
                {"questions": [{"question": "Language?", "header": "Lang",
                                "options": ["Python", "Go"]},
                               {"question": "Name?", "header": "Name",
                                "options": ["app"]}]})}}]}, _USAGE

    with fake_server(script) as server:
        cfg = Config()
        m = ModelConfig(name="fake", model="fake", base_url=server.base_url, api_key="k")
        cfg.add_model(m, make_default=True)
        agent = create_agent(model=m, config=cfg, cwd=str(tmp_path),
                             permissions=PermissionManager(auto_approve=True),
                             stream=False, verbose=False)
        app = CodedTUI(agent)
        async with app.run_test() as pilot:
            app.query_one("#prompt", Input).value = "start"
            await pilot.press("enter")
            for _ in range(80):
                if isinstance(app.screen, QuestionModal):
                    break
                await pilot.pause()
            assert isinstance(app.screen, QuestionModal)
            await pilot.press("2", "down", "w", "e", "b", "enter")
            assert app.screen.chooser.on_review
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
    tool_msg = next(m for m in requests[-1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "The user answered:\n- Language? → Go\n- Name? → web"
