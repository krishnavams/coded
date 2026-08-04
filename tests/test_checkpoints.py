"""Tests for checkpoint/undo and the move/delete tools."""

from __future__ import annotations

from coded.checkpoints import CheckpointManager
from coded.permissions import PermissionManager
from coded.tools import build_registry
from coded.tools.base import ToolContext


def _ctx(tmp_path, cp):
    return ToolContext(cwd=str(tmp_path), permissions=PermissionManager(auto_approve=True),
                       config=None, checkpoints=cp)


def _tool(name):
    return build_registry().get(name)


def test_undo_restores_edit(tmp_path):
    cp = CheckpointManager(str(tmp_path))
    ctx = _ctx(tmp_path, cp)
    f = tmp_path / "a.txt"
    f.write_text("original\n")

    cp.begin("edit a")
    _tool("edit").run({"path": "a.txt", "old_string": "original", "new_string": "changed"}, ctx)
    assert f.read_text() == "changed\n"

    msg = cp.undo_last()
    assert "restored" in msg
    assert f.read_text() == "original\n"


def test_undo_removes_created_file(tmp_path):
    cp = CheckpointManager(str(tmp_path))
    ctx = _ctx(tmp_path, cp)
    cp.begin("create b")
    _tool("write").run({"path": "b.txt", "content": "new file"}, ctx)
    assert (tmp_path / "b.txt").exists()

    cp.undo_last()
    assert not (tmp_path / "b.txt").exists()


def test_move_and_delete_tools(tmp_path):
    cp = CheckpointManager(str(tmp_path))
    ctx = _ctx(tmp_path, cp)
    (tmp_path / "src.txt").write_text("hi")
    r = _tool("move").run({"source": "src.txt", "destination": "sub/dst.txt"}, ctx)
    assert not r.is_error
    assert (tmp_path / "sub" / "dst.txt").read_text() == "hi"
    assert not (tmp_path / "src.txt").exists()

    cp.begin("delete")
    d = _tool("delete").run({"path": "sub/dst.txt"}, ctx)
    assert not d.is_error
    assert not (tmp_path / "sub" / "dst.txt").exists()
    cp.undo_last()
    assert (tmp_path / "sub" / "dst.txt").read_text() == "hi"


def test_undo_steps_back_through_groups(tmp_path):
    cp = CheckpointManager(str(tmp_path))
    ctx = _ctx(tmp_path, cp)
    f = tmp_path / "c.txt"
    f.write_text("v0\n")
    cp.begin("to v1")
    _tool("edit").run({"path": "c.txt", "old_string": "v0", "new_string": "v1"}, ctx)
    cp.begin("to v2")
    _tool("edit").run({"path": "c.txt", "old_string": "v1", "new_string": "v2"}, ctx)
    assert f.read_text() == "v2\n"
    cp.undo_last()
    assert f.read_text() == "v1\n"
    cp.undo_last()
    assert f.read_text() == "v0\n"
