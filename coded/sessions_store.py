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
            ))
        metas.sort(key=lambda m: m.updated, reverse=True)
        return metas

    def new_id(self) -> str:
        return _new_id()
