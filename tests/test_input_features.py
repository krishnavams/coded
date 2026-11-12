"""REPL input features: @mentions, Esc to stop, clipboard images, keys."""

from __future__ import annotations

import io
import os
import sys

import pytest

from coded.agent import create_agent
from coded.config import Config, ModelConfig
from coded.mentions import MentionCompleter, expand_mentions, list_paths, rank_paths
from coded.permissions import PermissionManager


def _files(tmp_path):
    (tmp_path / "app.py").write_text("a = 1\nb = 2\nc = 3\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "util.py").write_text("def f():\n    pass\n")
    (tmp_path / "my notes.md").write_text("hello\n")
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2\3")
    return tmp_path


# -- expanding mentions ---------------------------------------------------------

def test_a_mention_attaches_the_file_with_line_numbers(tmp_path):
    exp = expand_mentions("explain @app.py please", str(_files(tmp_path)))
    assert exp.attached == ["app.py"]
    assert exp.text.startswith("explain @app.py please")
    assert '<file path="app.py">' in exp.text and "     2\tb = 2" in exp.text


def test_line_ranges_folders_quotes_and_punctuation(tmp_path):
    cwd = str(_files(tmp_path))
    exp = expand_mentions("see @app.py#L2-3, @src/ and @\"my notes.md\".", cwd)
    assert exp.attached == ["app.py#L2-3", "src/", "my notes.md"]
    assert "     2\tb = 2" in exp.text and "a = 1" not in exp.text
    assert "util.py" in exp.text and "hello" in exp.text


def test_things_that_are_not_mentions_are_left_alone(tmp_path):
    cwd = str(_files(tmp_path))
    for text in ("mail me@app.py", "@missing.py", "x @", "@decorator\ndef f(): ..."):
        exp = expand_mentions(text, cwd)
        assert exp.text == text and not exp.attached


def test_images_go_to_vision_models_and_binaries_are_not_inlined(tmp_path):
    cwd = str(_files(tmp_path))
    exp = expand_mentions("what is @shot.png and @blob.bin", cwd)
    assert exp.images == [os.path.join(cwd, "shot.png")]
    assert "binary file" in exp.text
    blind = expand_mentions("what is @shot.png", cwd, vision=False)
    assert not blind.images and "cannot see images" in blind.text


def test_extra_dirs_are_searched_too(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "lib.py").write_text("x = 1\n")
    exp = expand_mentions("check @lib.py", str(_files(tmp_path)), dirs=[str(other)])
    assert exp.attached == ["lib.py"]


def test_the_agent_attaches_mentions_for_every_front_end(tmp_path, fake_server):
    _files(tmp_path)
    seen = []

    def script(body):
        seen.append(body["messages"][-1]["content"])
        return {"role": "assistant", "content": "ok"}, None

    cfg = Config()
    model = ModelConfig(name="fake", model="fake", base_url="", api_key="k")
    with fake_server(script) as server:
        model.base_url = server.base_url
        cfg.add_model(model, make_default=True)
        agent = create_agent(model=model, config=cfg, cwd=str(tmp_path),
                             permissions=PermissionManager(), stream=False, verbose=False)
        agent.run("explain @src/util.py")
    assert "def f():" in seen[0] and "<mentioned-files>" in seen[0]


# -- completion ----------------------------------------------------------------

def test_paths_are_listed_and_ranked(tmp_path):
    _files(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    paths = list_paths(str(tmp_path))
    assert "src/util.py" in paths and "src/" in paths
    assert not any(p.startswith((".git", "node_modules")) for p in paths)
    assert rank_paths("util", paths)[0] == "src/util.py"
    assert rank_paths("sutl", paths) == ["src/util.py"]  # letters in order


def test_the_completer_only_answers_after_an_at_sign(tmp_path):
    from prompt_toolkit.document import Document

    _files(tmp_path)
    comp = MentionCompleter(str(tmp_path))
    got = [c.text for c in comp.get_completions(Document("look at @uti"), None)]
    assert got == ["@src/util.py"]
    quoted = [c.text for c in comp.get_completions(Document("@my"), None)]
    assert quoted == ['@"my notes.md"']
    assert not list(comp.get_completions(Document("look at uti"), None))


# -- Esc stops a turn ------------------------------------------------------------

@pytest.fixture
def keys():
    if os.name == "nt":
        pytest.skip("POSIX terminal reader")
    import pty

    from coded.typeahead import _PosixKeys

    master, slave = pty.openpty()
    reader = _PosixKeys(fd=slave, out=io.StringIO())
    yield master, reader
    reader.close()
    os.close(master)
    os.close(slave)


def test_a_lone_escape_stops_but_arrow_keys_do_not(keys):
    from coded.typeahead import ESCAPE

    master, reader = keys
    os.write(master, b"\x1b[A")      # arrow up: an escape sequence
    assert reader(0.5) is None
    os.write(master, b"\x1b")
    assert reader(0.5) == ESCAPE


def test_keys_are_collected_into_lines_with_editing(keys):
    master, reader = keys
    os.write(master, b"helo\x7flo there\r")
    assert reader(0.5) == "hello there"
    os.write(master, b"half-typ")
    assert reader(0.5) is None and "".join(reader.buf) == "half-typ"


def test_the_terminal_is_restored(keys):
    import termios

    master, reader = keys
    before = termios.tcgetattr(reader.fd)
    reader(0.01)
    assert termios.tcgetattr(reader.fd)[3] & termios.ICANON == 0
    reader.close()
    after = termios.tcgetattr(reader.fd)
    # Line mode and echo come back exactly. The kernel may flip bits it owns
    # (macOS sets PENDIN on the way back to canonical mode), so compare the
    # modes we change, not the whole flag word.
    mask = termios.ICANON | termios.ECHO | termios.ISIG
    assert after[3] & mask == before[3] & mask
    assert after[:3] == before[:3] and after[6] == before[6]


def test_escape_stops_the_turn_or_answers_no_to_a_waiting_prompt():
    import threading

    from coded.typeahead import TypeAhead

    stopped = []
    ta = TypeAhead(poll=lambda _t: None, on_escape=lambda: stopped.append(1))
    ta.escape()
    assert stopped == [1]

    answer = []
    t = threading.Thread(target=lambda: answer.append(ta.ask(timeout=2)))
    t.start()
    while not ta._want_answer.is_set():
        pass
    ta.escape()
    t.join()
    assert answer == ["n"] and stopped == [1]


# -- clipboard -------------------------------------------------------------------

def test_clipboard_commands_per_platform():
    from coded.images import clipboard_commands

    have = {"wl-paste", "xclip", "pngpaste", "powershell"}
    which = lambda name: name if name in have else None  # noqa: E731
    linux = clipboard_commands("/t/x.png", "linux", which)
    assert [c[0][0] for c in linux] == ["wl-paste", "xclip"]
    mac = clipboard_commands("/t/x.png", "darwin", which)
    assert mac[0][0] == ["pngpaste", "/t/x.png"] and mac[1][0][0] == "osascript"
    assert clipboard_commands("/t/x.png", "win32", which)[0][0][0] == "powershell"
    assert clipboard_commands("/t/x.png", "linux", lambda n: None) == []


def test_grab_clipboard_image_saves_a_png_or_returns_none(tmp_path):
    from types import SimpleNamespace

    from coded.images import grab_clipboard_image

    png = b"\x89PNG\r\n\x1a\nDATA"
    ok = lambda argv, **kw: SimpleNamespace(returncode=0, stdout=png)  # noqa: E731
    path = grab_clipboard_image(str(tmp_path), platform="linux",
                                which=lambda n: n == "xclip", run=ok)
    assert path and open(path, "rb").read() == png
    text = lambda argv, **kw: SimpleNamespace(returncode=0, stdout=b"just text")  # noqa: E731
    assert grab_clipboard_image(str(tmp_path), platform="linux",
                                which=lambda n: n == "xclip", run=text) is None


# -- the REPL wiring -------------------------------------------------------------

def _repl(tmp_path, **cfg_kw):
    from coded.repl import Repl

    cfg = Config(**cfg_kw)
    m = ModelConfig(name="m", model="m", base_url="http://x/v1", api_key="k")
    cfg.add_model(m, make_default=True)
    agent = create_agent(model=m, config=cfg, cwd=str(tmp_path),
                         permissions=PermissionManager(), stream=False, verbose=False)
    return Repl(agent, cfg)


def test_repl_keys_and_vi_mode(tmp_path, headless):
    from prompt_toolkit.enums import EditingMode

    repl = _repl(tmp_path)
    assert repl.prompt.editing_mode == EditingMode.EMACS
    keys = {tuple(k.value if hasattr(k, "value") else k for k in b.keys)
            for b in repl.prompt.key_bindings.bindings}
    for wanted in (("escape", "c-m"), ("c-j",), ("c-g",), ("c-v",)):
        assert wanted in keys, wanted
    repl._handle_slash("/vim")
    assert repl.prompt.editing_mode == EditingMode.VI
    assert _repl(tmp_path, editing_mode="vi").prompt.editing_mode == EditingMode.VI


def test_paste_command_attaches_the_clipboard_image(tmp_path, headless, monkeypatch):
    repl = _repl(tmp_path)
    monkeypatch.setattr("coded.images.grab_clipboard_image", lambda: "/tmp/clip.png")
    repl._handle_slash("/paste")
    assert repl._pending_images == ["/tmp/clip.png"]
    monkeypatch.setattr("coded.images.grab_clipboard_image", lambda: None)
    repl._handle_slash("/paste")
    assert repl._pending_images == ["/tmp/clip.png"]
