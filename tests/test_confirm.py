"""Yes/no questions outside a turn use the same choice panel as permission prompts."""

from __future__ import annotations

import io

import pytest

from coded import confirm, ui
from coded.typeahead import KEY


@pytest.fixture
def con(monkeypatch):
    c = ui.TranscriptConsole(width=100, record=True, file=io.StringIO(), color_system=None)
    monkeypatch.setattr(ui, "console", c)
    return c


def _lines(*answers):
    it = iter(answers)

    def read(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return read


@pytest.mark.parametrize("typed,default,want", [
    ("y", False, True), ("yes", False, True), ("n", True, False), ("1", False, True),
    ("2", True, False), ("", True, True), ("", False, False),
])
def test_line_mode_answers(con, typed, default, want):
    assert confirm.yes_no("Trust this project?", "Apply these settings?", detail="a [/x] b",
                          default=default, read=_lines(typed)) is want
    shown = con.export_text()
    assert "Trust this project?" in shown and "Apply these settings?" in shown
    assert "a [/x] b" in shown                       # detail shown verbatim
    assert ("→ Yes" if want else "→ No") in shown    # the answer stays in the scrollback


def test_line_mode_asks_again_and_end_of_input_is_no(con):
    assert confirm.yes_no("T", "Q?", read=_lines("maybe", "3", "y")) is True
    assert "Type a number from 1 to 2" in con.export_text()
    assert confirm.yes_no("T", "Q?", default=True, read=_lines()) is False   # EOF = no


class _Keys:
    """A key-mode reader stand-in, as the terminal readers hand keys over."""

    def __init__(self, *keys):
        self.keys = list(keys)
        self.keys_mode = False
        self.closed = False

    def __call__(self, _timeout):
        if not self.keys:
            raise AssertionError("asked for more keys than typed")
        k = self.keys.pop(0)
        if k is KeyboardInterrupt:
            raise KeyboardInterrupt
        return KEY + k

    def close(self):
        self.closed = True


@pytest.mark.parametrize("keys,default,want", [
    (["enter"], True, True),              # Enter on the default
    (["enter"], False, False),
    (["up", "enter"], False, True),       # move to Yes
    (["down", "enter"], True, False),
    (["escape"], True, False),            # Esc = no
    ([KeyboardInterrupt], True, False),   # Ctrl-C = no
])
def test_key_mode_is_the_same_panel_as_permission_prompts(con, monkeypatch, keys, default, want):
    reader = _Keys(*keys)
    monkeypatch.setattr(confirm, "_key_reader", lambda: reader)
    assert confirm.yes_no("Mission · shell commands", "Run shell commands?",
                          default=default) is want
    assert reader.closed and reader.keys_mode     # terminal restored, key mode used


def test_no_old_style_prompts_are_left():
    """Every yes/no goes through the panel: no bare [y/N] / [Y/n] prompts."""
    import pathlib
    import re

    root = pathlib.Path(confirm.__file__).parent
    offenders = [f"{p.name}:{n}" for p in root.rglob("*.py")
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if re.search(r"\[(y/N|Y/n)\]|\((Y/n|y/N)\)|'Y/n' if|'y/N' if", line)
                 and p.name != "config_wizard.py"]
    assert offenders == []
