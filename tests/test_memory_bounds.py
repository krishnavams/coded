"""Big output and big files must not be held whole in memory: a sub-agent
that greps a data folder or runs a noisy command would otherwise grow coded
until the OS kills it (`zsh: killed coded`)."""

from __future__ import annotations

import sys

from coded import shells
from coded.background import BackgroundShells
from coded.permissions import PermissionManager
from coded.tools import files, search
from coded.tools.base import ToolContext


def _ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True),
                       config=None)


def test_run_keeps_the_head_and_tail_of_huge_output(tmp_path):
    code = "import sys; sys.stdout.write('A' * 3_000_000 + 'MID' + 'Z' * 3_000_000)"
    out = shells.run([sys.executable, "-c", code], cwd=str(tmp_path), timeout=60,
                     max_bytes=1_000_000)
    assert out.returncode == 0
    assert len(out.stdout) < 1_100_000
    assert out.stdout.startswith("A") and out.stdout.endswith("Z")
    assert "bytes of output omitted" in out.stdout and "MID" not in out.stdout


def test_run_small_output_and_input_are_unchanged(tmp_path):
    code = "import sys; print(sys.stdin.read().upper()); print('err', file=sys.stderr)"
    out = shells.run([sys.executable, "-c", code], cwd=str(tmp_path), timeout=60,
                     input="héllo\n")
    assert out.stdout == "HÉLLO\n\n" and out.stderr == "err\n"


def test_grep_skips_files_too_big_to_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "GREP_MAX_FILE_BYTES", 1000)
    (tmp_path / "big.log").write_text("needle\n" * 500)
    (tmp_path / "small.py").write_text("needle = 1\n")
    res = search.GrepTool().run({"pattern": "needle"}, _ctx(tmp_path))
    assert "small.py:1" in res.content and "big.log:" not in res.content
    assert "skipped 1 file(s)" in res.content


def test_read_streams_a_huge_text_file(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "READ_STREAM_BYTES", 100)
    p = tmp_path / "huge.log"
    p.write_text("".join(f"line {i}\n" for i in range(1, 501)))
    res = files.ReadTool().run({"path": str(p), "offset": 10, "limit": 3}, _ctx(tmp_path))
    assert "500 lines" in res.content
    assert "    10\tline 10" in res.content and "    12\tline 12" in res.content
    assert "line 13" not in res.content and "continue with offset=13" in res.content


def test_background_read_returns_only_the_newest_output(tmp_path, monkeypatch):
    import coded.background as bg

    monkeypatch.setattr(bg, "BG_READ_MAX_BYTES", 100)
    shells_ = BackgroundShells()
    sid = shells_.start([sys.executable, "-c", "print('x' * 1000 + 'END')"], str(tmp_path), "t")
    shells_.get(sid).proc.wait(timeout=30)
    text, status = shells_.read(sid)
    assert "earlier bytes not shown" in text and text.rstrip().endswith("END")
    assert len(text) < 200 and status == "exited 0"
    shells_.kill_all()
