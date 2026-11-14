"""Long work says how it ended — "Task completed" or "Task stopped: <why>" —
through the channels you pick (desktop, voice, bell, line); /notify turns it
on or off. Short requests stay quiet; missions always announce."""

from __future__ import annotations

import io

import pytest

from coded import notify, ui
from coded.events import EventLog
from coded.notify import Notifier, outcome_of, parse_channels, voice_command


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _notifier(channels="all", after=30):
    clock, sent, said, lines = _Clock(), [], [], []
    n = Notifier(channels, after=after, clock=clock,
                 sender=lambda title, msg, mode: sent.append((title, msg, mode)) or mode,
                 speaker=lambda text: said.append(text) or True,
                 liner=lambda kind, text: lines.append((kind, text)))
    return n, clock, sent, said, lines


# -- channels and the switch ---------------------------------------------------------

@pytest.mark.parametrize("value,want", [
    ("bell", ["bell"]), ("desktop,voice", ["desktop", "voice"]), ("all", list(notify.CHANNELS)),
    (["line", "nope", "line"], ["line"]), ("off", []), ("", []), (None, ["bell"]),
])
def test_channels_parse(value, want):
    assert parse_channels(value) == want


def test_notify_on_off_and_choosing_channels():
    n, *_ = _notifier("bell")
    assert "bell" in n.set([]) and n.enabled
    assert "off" in n.set(["off"]) and not n.enabled and n.mode == "off"
    assert "bell" in n.set(["on"]) and n.enabled
    assert "desktop, voice" in n.set(["desktop", "voice"])
    assert n.set(["all"]).count(",") == 3
    assert "desktop" in n.set(["bogus"])          # nothing valid: unchanged
    off = Notifier("off")
    assert not off.enabled and off.set(["on"]).startswith("Notifications on: bell")


# -- what is said, and when ---------------------------------------------------------------

def test_long_work_announces_on_every_channel_short_work_stays_quiet():
    n, clock, sent, said, lines = _notifier("all")
    n.start()
    clock.t = 5
    assert n.outcome("completed", label="quick one") == []
    n.start()
    clock.t = 125
    used = n.outcome("completed", label="build the reports page")
    assert used == ["line", "desktop", "bell", "voice"]
    assert lines == [("completed", "Task completed (2m 00s) — build the reports page")]
    assert sent[0] == ("coded: Task completed", "build the reports page", "desktop")
    assert said == ["Task completed"]


def test_a_stop_says_why_and_forced_announcements_ignore_the_threshold():
    n, clock, sent, said, lines = _notifier("line,voice")
    n.start()
    n.outcome("stopped", "it reached the step limit", "refactor", force=True)
    assert lines[-1][0] == "stopped"
    assert lines[-1][1].startswith("Task stopped: it reached the step limit")
    assert said[-1] == "Task stopped. it reached the step limit" and sent == []
    n.set(["off"])
    n.start()
    clock.t = 999
    assert n.outcome("completed", force=True) == []      # off means off, even forced


def test_waiting_for_an_answer_calls_you_over_on_long_work():
    n, clock, sent, said, _ = _notifier("desktop,voice")
    n.start()
    clock.t = 60
    assert n.waiting("Run shell command") == "desktop"
    assert said == ["coded needs your answer"]


# -- why it ended ---------------------------------------------------------------------------

class _Agent:
    def __init__(self, events=(), over=None):
        self.events = EventLog(list(events))
        self._over = over

    def over_budget(self):
        return self._over


TURN = {"kind": "turn", "prompt": "x"}


@pytest.mark.parametrize("agent,ok,error,interrupted,want", [
    (_Agent([TURN]), True, "", False, ("completed", "")),
    (_Agent([TURN]), False, "Connection refused\nmore", False,
     ("stopped", "error: Connection refused")),
    (_Agent([TURN]), True, "", True, ("stopped", "you stopped it")),
    (_Agent([TURN, {"kind": "interrupt"}]), True, "", False, ("stopped", "you stopped it")),
    (_Agent([TURN, {"kind": "max_turns", "reason": "it does not look like progress: x"}]),
     True, "", False, ("stopped", "it was not making progress")),
    (_Agent([TURN, {"kind": "max_turns", "reason": ""}]), True, "", False,
     ("stopped", "it reached the step limit")),
    (_Agent([TURN], over="over"), True, "", False,
     ("stopped", "the spending limit was reached")),
    (_Agent([{"kind": "max_turns"}, TURN]), True, "", False, ("completed", "")),  # old request
])
def test_outcome_of(agent, ok, error, interrupted, want):
    assert outcome_of(agent, ok, error, interrupted) == want


# -- speaking --------------------------------------------------------------------------------

def test_voice_commands_per_platform_and_only_plain_words():
    have = lambda name: name                                   # noqa: E731
    assert voice_command("Task completed", "darwin", have) == ["say", "Task completed"]
    assert voice_command("hi", "linux", lambda n: n if n == "espeak" else None) == ["espeak", "hi"]
    win = voice_command("Task stopped", "win32", lambda n: n if n == "powershell" else None)
    assert win[0] == "powershell" and "'Task stopped'" in win[-1]
    evil = voice_command("x'; rm -rf / #`$(boom)", "win32", have)
    assert "'" not in evil[-1].split("Speak(", 1)[1][1:-2] and "$" not in evil[-1].split("Speak(")[1]
    assert voice_command("hi", "linux", lambda n: None) is None
    assert voice_command("", "darwin", have) is None


# -- the front-ends ---------------------------------------------------------------------------

@pytest.fixture
def repl(tmp_path, fake_server, monkeypatch, headless):
    from coded.config import Config
    from coded.repl import Repl
    from tests.test_agent_e2e import _make_agent

    con = ui.TranscriptConsole(width=120, record=True, file=io.StringIO(), color_system=None)
    monkeypatch.setattr(ui, "console", con)
    with fake_server(lambda b: ({"role": "assistant", "content": "ok"}, None)) as server:
        r = Repl(_make_agent(tmp_path, server.base_url), Config())
        r.notifier.channels, r.notifier.after = ["line"], 0     # every request is "long"
        yield r


def test_the_repl_announces_completed_and_stopped(repl):
    with repl._busy("build it"):
        repl.agent.run("build it")
    with pytest.raises(KeyboardInterrupt):
        with repl._busy("refactor"):
            raise KeyboardInterrupt
    with pytest.raises(RuntimeError):
        with repl._busy("deploy"):
            raise RuntimeError("endpoint down")
    shown = ui.console.export_text()
    assert "✓ Task completed" in shown and "— build it" in shown
    assert "■ Task stopped: you stopped it" in shown
    assert "■ Task stopped: error: endpoint down" in shown


def test_the_notify_command_switches_it(repl):
    repl._handle_slash("/notify off")
    with repl._busy("quiet"):
        pass
    repl._handle_slash("/notify on")
    repl._handle_slash("/notify test")
    shown = ui.console.export_text()
    assert "Notifications are off" in shown and "quiet" not in shown.split("off")[-1]
    assert "Sent: line" in shown and "a test notification" in shown
    assert repl._intercept_midturn("/notify off") and not repl.notifier.enabled


pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_the_tui_announces_in_the_transcript(tmp_path, fake_server):
    from coded.tui import CodedTUI, StatusNote
    from tests.test_tui import _agent

    with fake_server(lambda b: ({"role": "assistant", "content": "ok"}, None)) as server:
        app = CodedTUI(_agent(tmp_path, server.base_url))
        app.notifier.channels, app.notifier.after = ["line"], 0
        async with app.run_test() as pilot:
            app._submit("build it")
            await app.workers.wait_for_complete()
            await pilot.pause()
            notes = [str(w.render()) for w in app.query(StatusNote)]
            assert any("✓ Task completed" in n and "build it" in n for n in notes)
            app._submit("/notify off")
            await pilot.pause()
            assert not app.notifier.enabled
