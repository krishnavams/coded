"""Persist and restore conversations so sessions survive across runs.

Sessions are saved as JSON under ~/.config/coded/sessions/<id>.json (or
$CODED_SESSIONS_DIR). Each file records the messages, model, usage, cwd, and
timestamps. `coded --continue` loads the most recent session for the current
directory; `coded --resume <id>` loads a specific one.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from coded.config import user_config_path
from coded.llm import Usage
from coded.session import Session


def sessions_dir() -> Path:
    env = os.environ.get("CODED_SESSIONS_DIR")
    base = Path(env) if env else user_config_path().parent / "sessions"
    return base


def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


@dataclass
class SessionMeta:
    id: str
    cwd: str
    model: str
    updated: float
    turns: int
    messages: int
    preview: str = ""  # the first request typed in it


def first_request(messages: list) -> str:
    """The first message the person typed (coded's own [verify]/[memory] notes skipped)."""
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content
                               if isinstance(p, dict) and p.get("type") == "text")
        text = " ".join(str(content or "").split())
        if text and not text.startswith(("[verify]", "[memory]")):
            return text
    return ""


INTERRUPTED_RESULT = "[interrupted: coded stopped before this tool call finished]"


def close_tool_calls(messages: list) -> list:
    """Give every assistant tool call a result, so the history is valid again.

    A session saved while a step was in flight (or after Ctrl-C mid-tool) can
    end with tool calls that have no `tool` message; endpoints reject such a
    history, which would make a resumed session fail on its first request.
    """
    out: list = []
    pending: list = []

    def flush() -> None:
        for call_id in pending:
            out.append({"role": "tool", "tool_call_id": call_id,
                        "content": INTERRUPTED_RESULT})
        pending.clear()

    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else None
        if role == "tool":
            if msg.get("tool_call_id") in pending:
                pending.remove(msg.get("tool_call_id"))
            out.append(msg)
            continue
        flush()
        out.append(msg)
        if role == "assistant":
            pending.extend(tc.get("id") for tc in msg.get("tool_calls") or []
                           if isinstance(tc, dict) and tc.get("id"))
    flush()
    return out


class SessionStore:
    def __init__(self) -> None:
        self.dir = sessions_dir()

    # -- save ---------------------------------------------------------------
    def save(self, session: Session, *, session_id: str, cwd: str, model: str) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": session_id,
            "cwd": cwd,
            "model": model,
            "created": getattr(session, "_created", time.time()),
            "updated": time.time(),
            "turns": session.turns,
            "compactions": getattr(session, "compactions", 0),
            "usage": {
                "prompt_tokens": session.total_usage.prompt_tokens,
                "completion_tokens": session.total_usage.completion_tokens,
                "cached_tokens": session.total_usage.cached_tokens,
                "cost": session.cost,
            },
            "todos": [dict(t) for t in session.todos],
            "events": list(getattr(session, "events", None) or [])[-3000:],
            "system_prompt": session.system_prompt,
            "messages": session.messages,
        }
        # Atomic: saved after every step, so a kill mid-write must not leave a
        # truncated file in place of the last good one.
        path = self.dir / f"{session_id}.json"
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        return session_id

    # -- load ---------------------------------------------------------------
    def _read(self, path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load(self, session_id: str) -> Optional[Session]:
        data = self._read(self.dir / f"{session_id}.json")
        return self._to_session(data) if data else None

    def _to_session(self, data: dict) -> Session:
        s = Session(system_prompt=data.get("system_prompt", ""))
        s.messages = close_tool_calls(data.get("messages") or s.messages)
        u = data.get("usage") or {}
        s.total_usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                              u.get("cached_tokens", 0))
        s.cost = float(u.get("cost", 0.0) or 0.0)
        s._priced = bool(u.get("cost"))
        s.turns = data.get("turns", 0)
        s.compactions = data.get("compactions", 0)
        s.todos = [dict(t) for t in data.get("todos") or []
                   if isinstance(t, dict) and t.get("content")]
        s.events = [e for e in data.get("events") or [] if isinstance(e, dict) and e.get("kind")]
        return s

    def latest(self, cwd: Optional[str] = None) -> Optional[str]:
        metas = self.list()
        if cwd:
            metas = [m for m in metas if m.cwd == cwd] or metas
        return metas[0].id if metas else None

    def list(self) -> List[SessionMeta]:
        if not self.dir.is_dir():
            return []
        metas = []
        for p in self.dir.glob("*.json"):
            data = self._read(p)
            if not data:
                continue
            metas.append(SessionMeta(
                id=data.get("id", p.stem),
                cwd=data.get("cwd", ""),
                model=data.get("model", ""),
                updated=data.get("updated", 0),
                turns=data.get("turns", 0),
                messages=len(data.get("messages", [])),
                preview=first_request(data.get("messages") or []),
            ))
        metas.sort(key=lambda m: m.updated, reverse=True)
        return metas

    def new_id(self) -> str:
        return _new_id()


class SessionKeeper:
    """The saved session a running front-end writes to, and switching it.

    The CLI's autosave, `--resume`/`--continue` and the REPL/TUI `/resume`
    all go through one keeper, so a resumed session keeps saving under its
    own id instead of the one the process started with.
    """

    def __init__(self, cwd: str, model: str, store: Optional[SessionStore] = None,
                 session_id: Optional[str] = None) -> None:
        self.store = store or SessionStore()
        self.cwd = cwd
        self.model = model
        self.id = session_id or self.store.new_id()

    def save(self, agent) -> None:
        agent.session.todos = [dict(t) for t in agent.todos.items]
        try:
            self.store.save(agent.session, session_id=self.id, cwd=self.cwd, model=self.model)
        except OSError:
            pass

    def here(self) -> List[SessionMeta]:
        """Saved sessions for this folder, newest first (the current one left out)."""
        return [m for m in self.store.list() if m.cwd == self.cwd and m.id != self.id]

    def resolve(self, arg: str) -> Optional[str]:
        """A session id from `/resume N` (as listed by `here`), an id or a unique id prefix."""
        arg = arg.strip()
        if arg.isdigit() and len(arg) < 4:
            metas = self.here()
            n = int(arg)
            return metas[n - 1].id if 1 <= n <= len(metas) else None
        ids = [m.id for m in self.store.list()]
        if arg in ids:
            return arg
        matches = [i for i in ids if i.startswith(arg)]
        return matches[0] if len(matches) == 1 else None

    def resume(self, agent, session_id: str) -> Optional[Session]:
        """Load `session_id` into `agent` and save to it from now on; None if missing."""
        loaded = self.store.load(session_id)
        if loaded is None:
            return None
        agent.session = loaded
        agent.todos.items = [dict(t) for t in loaded.todos]
        self.id = session_id
        return loaded

    @staticmethod
    def has_content(agent) -> bool:
        return bool(agent.session.turns or len(agent.session.messages) > 1)

    def switch(self, agent, session_id: str) -> Optional[Session]:
        """`/resume`: save the open conversation (if it has any), then load another."""
        if self.has_content(agent):
            self.save(agent)
        return self.resume(agent, session_id)


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


RESUME_LIMIT = 20


def resume_command(agent, keeper: Optional[SessionKeeper], arg: str):
    """`/resume [N | id]` for the REPL and TUI.

    Returns (message, renderable, resumed): `message` is plain text for a
    note (or ""), `renderable` a rich Table listing sessions (or None).
    """
    if keeper is None:
        return "Saved sessions are off in this front-end.", None, False
    arg = arg.strip()
    if not arg:
        metas = keeper.here()
        if not metas:
            return ("No other saved sessions for this folder. `coded sessions` lists "
                    "every folder's.", None, False)
        from rich.table import Table
        from rich.text import Text

        from coded import ui

        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        now = time.time()
        for n, m in enumerate(metas[:RESUME_LIMIT], 1):
            preview = m.preview or "(no request yet)"
            preview = preview[:70] + ("…" if len(preview) > 70 else "")
            table.add_row(Text(str(n), style=ui.ACCENT), Text(preview),
                          Text(f"{_age(now - m.updated)} · {m.turns} req · {m.id}",
                               style=ui.MUTED))
        more = len(metas) - RESUME_LIMIT
        tail = f" ({more} older not shown; /resume <id> reaches any.)" if more > 0 else ""
        return ("/resume N reopens one; the current conversation stays saved." + tail,
                table, False)
    target = keeper.resolve(arg)
    if target is None:
        return (f"No saved session '{arg}'. /resume lists this folder's; "
                "`coded sessions` lists all.", None, False)
    if target == keeper.id:
        return "That is the session you are in.", None, False
    previous = keeper.id if keeper.has_content(agent) else None
    loaded = keeper.switch(agent, target)
    if loaded is None:
        return f"Could not read saved session '{target}'.", None, False
    last = ""
    for msg in reversed(loaded.messages):
        if msg.get("role") == "user":
            last = first_request([msg])
            if last:
                break
    note = (f"Resumed session {target} ({loaded.turns} request(s), "
            f"{len(loaded.messages)} messages).")
    if last:
        note += f" Last request: {last[:80]}{'…' if len(last) > 80 else ''}"
    if loaded_cwd := next((m.cwd for m in keeper.store.list() if m.id == target), ""):
        if loaded_cwd != keeper.cwd:
            note += f" (It was started in {loaded_cwd}; tools still work in {keeper.cwd}.)"
    if previous:
        note += f" The previous conversation is saved as {previous}."
    return note, None, True
