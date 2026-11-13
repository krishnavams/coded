"""Time-travel: a session, step by step, with the file states between steps.

`/timeline` lists the requests of this conversation; `/timeline N` replays one:
its prompt, the assistant's text, every tool call with its arguments, outcome
(from the event log: failed, denied, how long) and result, and the files it
changed with their diff. `/timeline restore N` shows exactly what going back to
just before request N would change, then goes there through `/rewind`'s path.
`coded sessions show <id>` replays a saved session (view only).

File states come from the per-turn checkpoints: the state of a file *after*
turn k is the backup taken by the next later turn that touched it (it was
saved just before that turn changed it), or the file on disk when no later
turn did. Two limits are said out loud wherever they apply: `bash` edits are
never snapshotted, and the model's hidden reasoning is not stored.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.box import ROUNDED
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from coded import ui

_RESULT_LINES = 6
_DIFF_LINES = 80
BASH_NOTE = "bash edits are not snapshotted, so files changed through the shell are not shown"


@dataclass
class Step:
    kind: str                      # "text" | "tool"
    text: str = ""                 # assistant text, or the tool result
    name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    event: Optional[Dict[str, Any]] = None


@dataclass
class Turn:
    number: int
    prompt: str
    steps: List[Step]
    checkpoint: Optional[str] = None   # the checkpoint group id of this turn


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def _is_request(msg: Dict[str, Any]) -> bool:
    """A user message the person typed (not coded's own feedback or images)."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    text = content if isinstance(content, str) else (
        content[0].get("text", "") if isinstance(content, list) and content
        and isinstance(content[0], dict) else "")
    return not str(text).startswith(("[verify]", "[memory]", "[view_image]",
                                     "[Stop hook feedback", "[background"))


def _steps(messages: List[Dict[str, Any]], events: Dict[str, Dict[str, Any]]) -> List[Step]:
    results = {m.get("tool_call_id"): str(m.get("content") or "")
               for m in messages if m.get("role") == "tool"}
    steps: List[Step] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        if m.get("content"):
            steps.append(Step("text", text=str(m["content"])))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {"_raw": fn.get("arguments")}
            steps.append(Step("tool", text=results.get(tc.get("id"), "(no result)"),
                              name=fn.get("name", "?"), args=args if isinstance(args, dict) else {},
                              call_id=tc.get("id", ""), event=events.get(tc.get("id", ""))))
    return steps


def build(messages: List[Dict[str, Any]], events: List[Dict[str, Any]] = (),
          marks: Optional[List[Dict[str, Any]]] = None) -> List[Turn]:
    """Turns of a conversation. With the live agent's `marks` (turn_history)
    each turn knows its checkpoint; a saved session finds turns by message."""
    by_id = {e["id"]: e for e in events if e.get("kind") == "tool" and e.get("id")}
    if marks:
        starts = []
        for mark in marks:
            idx = next((i for i, m in enumerate(messages) if m is mark["message"]), None)
            if idx is not None:
                starts.append((idx, mark["text"], mark.get("checkpoint")))
    else:
        starts = [(i, str(m.get("content") if isinstance(m.get("content"), str)
                          else "(message with images)"), None)
                  for i, m in enumerate(messages) if _is_request(m)]
    turns = []
    for n, (idx, text, checkpoint) in enumerate(starts, 1):
        end = starts[n][0] if n < len(starts) else len(messages)
        turns.append(Turn(n, text, _steps(messages[idx + 1:end], by_id), checkpoint))
    return turns


def build_for(agent) -> List[Turn]:
    return build(agent.session.messages, agent.events.snapshot(), agent.turn_history())


# ---------------------------------------------------------------------------
# File states
# ---------------------------------------------------------------------------
def _groups(checkpoints) -> List[Dict[str, Any]]:
    try:
        return checkpoints.list_groups() if checkpoints is not None else []
    except Exception:  # noqa: BLE001
        return []


def _read_backup(checkpoints, group_id: str, record: Dict[str, Any]) -> Optional[str]:
    if not record.get("existed") or not record.get("backup"):
        return None
    try:
        return (checkpoints.root / group_id / record["backup"]).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None


def _current(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def turn_changes(checkpoints, group_id: Optional[str]) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """(path, before, after) for each file the turn changed. `before` is its
    checkpoint backup; `after` is the next later backup of the file, or the
    file on disk. None means "did not exist"."""
    if not group_id:
        return []
    groups = _groups(checkpoints)
    mine = next((g for g in groups if g.get("id") == group_id), None)
    if mine is None:
        return []
    later = [g for g in groups if str(g.get("id", "")) > group_id]
    out = []
    for rec in mine.get("files", []):
        path = rec.get("path", "")
        before = _read_backup(checkpoints, group_id, rec)
        after: Optional[str] = None
        for g in later:
            nxt = next((r for r in g.get("files", []) if r.get("path") == path), None)
            if nxt is not None:
                after = _read_backup(checkpoints, g["id"], nxt)
                break
        else:
            after = _current(path)
        out.append((path, before, after))
    return out


def restore_preview(checkpoints, group_id: Optional[str]) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """(path, now, after restore) for every file going back before this turn
    would change: the oldest backup at or after `group_id` wins."""
    if not group_id:
        return []
    seen: Dict[str, Optional[str]] = {}
    for g in _groups(checkpoints):
        if str(g.get("id", "")) < group_id:
            continue
        for rec in g.get("files", []):
            path = rec.get("path", "")
            if path not in seen:
                seen[path] = _read_backup(checkpoints, g["id"], rec)
    out = []
    for path, target in seen.items():
        now = _current(path)
        if now != target:
            out.append((path, now, target))
    return out


def _diff(path: str, before: Optional[str], after: Optional[str], cwd: str) -> Text:
    rel = ui._rel(path, cwd)
    if before == after:
        return Text(f"  {rel}: unchanged", style=ui.MUTED)
    if before is None:
        head = f"  {rel}: created"
    elif after is None:
        head = f"  {rel}: deleted"
    else:
        head = f"  {rel}: edited"
    lines = list(difflib.unified_diff((before or "").splitlines(True), (after or "").splitlines(True),
                                      fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    body = Text()
    body.append(head + "\n", style="bold")
    body.append_text(ui.diff_text("".join(lines), max_lines=_DIFF_LINES))
    return body


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _outcome(step: Step) -> Text:
    e = step.event or {}
    if e.get("denied"):
        return Text("denied", style=ui.WARN)
    if e.get("error"):
        return Text("failed", style=ui.ERR)
    if e:
        return Text("ok", style=ui.ACCENT2)
    return Text("—", style=ui.MUTED)


def render_list(turns: List[Turn]) -> Any:
    if not turns:
        return Text("Nothing to show yet: no requests in this conversation.", style=ui.MUTED)
    table = Table(box=ROUNDED, border_style=ui.ACCENT_DEEP, title="Timeline", title_justify="left",
                  title_style=f"bold {ui.ACCENT}", expand=True)
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("request", ratio=1, overflow="ellipsis", no_wrap=True)
    table.add_column("tools", justify="right", no_wrap=True)
    table.add_column("failed", justify="right", no_wrap=True)
    table.add_column("files", justify="right", no_wrap=True)
    for t in turns:
        tools = [s for s in t.steps if s.kind == "tool"]
        bad = sum(1 for s in tools if (s.event or {}).get("error"))
        table.add_row(str(t.number), ui._one_line(t.prompt, 90), str(len(tools)),
                      Text(str(bad), style=ui.ERR if bad else ui.MUTED),
                      "✎" if t.checkpoint else "")
    hint = Text("/timeline N replays request N · /timeline restore N goes back to just "
                "before it (shows what changes first)", style=ui.MUTED)
    return Group(table, hint)


def render_turn(turn: Turn, checkpoints, cwd: str) -> Any:
    parts: List[Any] = []
    head = Text()
    head.append(f"#{turn.number}  ", style=f"bold {ui.ACCENT}")
    head.append(turn.prompt.strip()[:600], style="bold")
    parts.append(Panel(head, border_style=ui.ACCENT, box=ROUNDED, title=f"[{ui.ACCENT}]Request[/]",
                       title_align="left"))
    for i, step in enumerate(turn.steps, 1):
        if step.kind == "text":
            parts.append(ui.answer_block(Text(step.text.strip()[:1500])))
            continue
        line = ui.tool_call_text(step.name, step.args, cwd)
        line.append("  ")
        line.append_text(_outcome(step))
        if step.event and step.event.get("ms") is not None:
            line.append(f"  {step.event['ms']}ms", style=ui.MUTED)
        parts.append(line)
        parts.append(ui.tool_result_text(step.text, bool((step.event or {}).get("error")),
                                         step.name, cwd, max_lines=_RESULT_LINES))
    changes = turn_changes(checkpoints, turn.checkpoint)
    files = Text("\nFiles changed by this request:", style=f"bold {ui.ACCENT}")
    parts.append(files)
    if changes:
        for path, before, after in changes:
            parts.append(_diff(path, before, after, cwd))
    else:
        parts.append(Text("  none recorded", style=ui.MUTED))
    parts.append(Text(f"  ({BASH_NOTE}; the model's hidden reasoning is not stored)",
                      style=ui.MUTED))
    return Group(*parts)


def render_restore(number: int, preview, cwd: str) -> Any:
    if not preview:
        return Text(f"Going back to before request #{number} changes no files (only the "
                    f"conversation). {BASH_NOTE}.", style=ui.MUTED)
    parts: List[Any] = [Text(f"Going back to just before request #{number} would change "
                             f"{len(preview)} file(s):", style="bold")]
    for path, now, target in preview:
        parts.append(_diff(path, now, target, cwd))
    parts.append(Text(f"({BASH_NOTE}.)", style=ui.MUTED))
    return Group(*parts)
