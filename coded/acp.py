"""Agent Client Protocol (ACP) server: lets editors (Zed, JetBrains, Neovim
plugins, …) drive coded as their agent.

Run ``coded acp``; the editor speaks newline-delimited JSON-RPC 2.0 on
stdin/stdout. Supported:

- ``initialize``, ``authenticate`` (no-op), ``session/new``, ``session/prompt``,
  ``session/set_mode`` (``default`` / ``plan``), ``session/cancel``.
- Agent → client: ``session/update`` notifications (``agent_message_chunk``,
  ``agent_thought_chunk``, ``tool_call``, ``tool_call_update``, ``plan``) and
  ``session/request_permission`` requests for risky tool calls.

Only text prompt content is used; images/resources are attached as text when
they carry any.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = 1

_TOOL_KIND = {
    "read": "read", "ls": "read", "glob": "search", "grep": "search",
    "semantic_search": "search", "edit": "edit", "multi_edit": "edit", "write": "edit",
    "apply_patch": "edit", "move": "move", "delete": "delete", "bash": "execute",
    "bash_output": "execute", "kill_shell": "execute", "web_fetch": "fetch",
    "web_search": "fetch", "task": "think", "todo_write": "think",
    "external_agent": "other",
}


class ACPServer:
    def __init__(self, make_agent: Callable[[str], Any],
                 read_line: Callable[[], str], write: Callable[[str], None]):
        self.make_agent = make_agent
        self._read_line = read_line
        self._write_raw = write
        self._write_lock = threading.Lock()
        self.sessions: Dict[str, Any] = {}
        self._pending: Dict[Any, Dict[str, Any]] = {}
        self._next_id = 0
        self._id_lock = threading.Lock()

    # -- transport ----------------------------------------------------------
    def _send(self, obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._write_lock:
            self._write_raw(line + "\n")

    def _respond(self, msg_id, result=None, error=None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        self._send(payload)

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send a request to the client and block until its response arrives."""
        with self._id_lock:
            self._next_id += 1
            msg_id = f"coded-{self._next_id}"
        slot: Dict[str, Any] = {"event": threading.Event()}
        self._pending[msg_id] = slot
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        slot["event"].wait(timeout)
        self._pending.pop(msg_id, None)
        if "error" in slot:
            raise RuntimeError(slot["error"].get("message", "client error"))
        return slot.get("result") or {}

    # -- main loop ----------------------------------------------------------
    def serve(self) -> None:
        while True:
            line = self._read_line()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._send({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}})
                continue
            if "method" in msg:
                self._dispatch(msg)
            elif msg.get("id") in self._pending:
                slot = self._pending[msg["id"]]
                if "error" in msg:
                    slot["error"] = msg["error"]
                else:
                    slot["result"] = msg.get("result")
                slot["event"].set()
        for slot in list(self._pending.values()):  # unblock waiters on EOF
            slot["error"] = {"message": "client disconnected"}
            slot["event"].set()

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method, params, msg_id = msg["method"], msg.get("params") or {}, msg.get("id")
        handler = {
            "initialize": self._initialize,
            "authenticate": lambda p: {},
            "session/new": self._new_session,
            "session/set_mode": self._set_mode,
        }.get(method)
        try:
            if method == "session/prompt":
                threading.Thread(target=self._prompt, args=(msg_id, params), daemon=True).start()
                return
            if method == "session/cancel":
                agent = self.sessions.get(params.get("sessionId"))
                if agent is not None:
                    agent.interrupt()
                return
            if handler is None:
                if msg_id is not None:
                    self._respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})
                return
            result = handler(params)
            if msg_id is not None:
                self._respond(msg_id, result)
        except Exception as exc:  # noqa: BLE001
            if msg_id is not None:
                self._respond(msg_id, error={"code": -32603, "message": str(exc)})

    # -- handlers -----------------------------------------------------------
    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from coded import __version__

        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
            },
            "agentInfo": {"name": "coded", "version": __version__},
            "authMethods": [],
        }

    def _new_session(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cwd = params.get("cwd") or os.getcwd()
        session_id = uuid.uuid4().hex
        agent = self.make_agent(cwd)
        agent.on_event = lambda ev, sid=session_id: self._on_event(sid, ev)
        agent.permissions.set_prompt(
            lambda title, detail, sid=session_id: self._ask_permission(sid, title, detail),
            threadsafe=True)
        self.sessions[session_id] = agent
        return {
            "sessionId": session_id,
            "modes": {
                "currentModeId": agent.mode,
                "availableModes": [
                    {"id": "default", "name": "Default", "description": "Ask before risky actions"},
                    {"id": "plan", "name": "Plan", "description": "Read-only research, then propose a plan"},
                ],
            },
        }

    def _set_mode(self, params: Dict[str, Any]) -> Dict[str, Any]:
        agent = self.sessions.get(params.get("sessionId"))
        if agent is None:
            raise ValueError("unknown session")
        mode = params.get("modeId")
        if mode not in ("default", "plan"):
            raise ValueError(f"unknown mode {mode!r}")
        agent.set_mode(mode)
        return {}

    def _prompt(self, msg_id, params: Dict[str, Any]) -> None:
        agent = self.sessions.get(params.get("sessionId"))
        if agent is None:
            self._respond(msg_id, error={"code": -32602, "message": "unknown session"})
            return
        text = _prompt_text(params.get("prompt") or [])
        try:
            agent.run(text)
        except Exception as exc:  # noqa: BLE001
            self._respond(msg_id, error={"code": -32603, "message": str(exc)})
            return
        self._respond(msg_id, {"stopReason": "cancelled" if agent._interrupted else "end_turn"})

    # -- agent events → session/update --------------------------------------
    def _update(self, session_id: str, update: Dict[str, Any]) -> None:
        self.notify("session/update", {"sessionId": session_id, "update": update})

    def _on_event(self, session_id: str, ev: Dict[str, Any]) -> None:
        kind = ev.get("type")
        if kind == "assistant_delta" and ev.get("text"):
            self._update(session_id, {"sessionUpdate": "agent_message_chunk",
                                      "content": {"type": "text", "text": ev["text"]}})
        elif kind == "reasoning_delta" and ev.get("text"):
            self._update(session_id, {"sessionUpdate": "agent_thought_chunk",
                                      "content": {"type": "text", "text": ev["text"]}})
        elif kind == "tool_call":
            name, args = ev.get("name", ""), ev.get("args") or {}
            update = {"sessionUpdate": "tool_call", "toolCallId": ev.get("id") or uuid.uuid4().hex,
                      "title": _tool_title(name, args), "kind": _TOOL_KIND.get(name, "other"),
                      "status": "in_progress", "rawInput": args}
            path = args.get("path")
            if isinstance(path, str):
                update["locations"] = [{"path": path}]
            self._update(session_id, update)
        elif kind == "tool_result":
            self._update(session_id, {
                "sessionUpdate": "tool_call_update", "toolCallId": ev.get("id", ""),
                "status": "failed" if ev.get("is_error") else "completed",
                "content": [{"type": "content",
                             "content": {"type": "text", "text": ev.get("content", "")}}],
            })
        elif kind == "diff" and ev.get("id"):
            self._update(session_id, {
                "sessionUpdate": "tool_call_update", "toolCallId": ev["id"],
                "content": [{"type": "content", "content": {"type": "text",
                                                            "text": "```diff\n" + ev.get("diff", "") + "\n```"}}],
            })
        elif kind == "todos":
            self._update(session_id, {"sessionUpdate": "plan", "entries": [
                {"content": t["content"], "priority": "medium", "status": t["status"]}
                for t in ev.get("items", [])]})
        elif kind == "status" and ev.get("text"):
            self._update(session_id, {"sessionUpdate": "agent_thought_chunk",
                                      "content": {"type": "text", "text": f"[{ev['text']}]\n"}})

    def _ask_permission(self, session_id: str, title: str, detail: str) -> str:
        try:
            result = self.call("session/request_permission", {
                "sessionId": session_id,
                "toolCall": {"toolCallId": uuid.uuid4().hex, "title": title,
                             "rawInput": {"detail": detail}},
                "options": [
                    {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "no", "name": "Reject", "kind": "reject_once"},
                ],
            })
        except RuntimeError:
            return "no"
        outcome = result.get("outcome") or {}
        if outcome.get("outcome") == "selected":
            return {"yes": "yes", "always": "always"}.get(outcome.get("optionId"), "no")
        return "no"


def _prompt_text(blocks: List[Dict[str, Any]]) -> str:
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "resource":
            res = b.get("resource") or {}
            if res.get("text"):
                parts.append(f"<file uri=\"{res.get('uri', '')}\">\n{res['text']}\n</file>")
        elif b.get("type") == "resource_link":
            parts.append(f"(referenced: {b.get('uri', '')})")
    return "\n\n".join(p for p in parts if p)


def _tool_title(name: str, args: Dict[str, Any]) -> str:
    for key in ("command", "path", "pattern", "query", "url", "description"):
        if isinstance(args.get(key), str):
            val = args[key]
            return f"{name}: {val[:80]}"
    return name


def main(argv: Optional[List[str]] = None) -> int:
    from coded import ui
    from coded.agent import create_agent
    from coded.config import ConfigError, load_config, model_from_overrides
    from coded.permissions import PermissionManager
    from coded.skills import discover_skills
    from coded.telemetry import tracer_from_config

    p = argparse.ArgumentParser(prog="coded acp",
                                description="Serve coded over the Agent Client Protocol (stdio).")
    p.add_argument("-m", "--model", help="Model alias to use.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--yolo", action="store_true", help="Auto-approve tool actions.")
    p.add_argument("--base-url")
    p.add_argument("--api-key")
    p.add_argument("--model-name")
    p.add_argument("--provider")
    args = p.parse_args(argv)

    ui.set_quiet(True)  # stdout carries JSON-RPC only

    def make_agent(cwd: str):
        cfg = load_config(cwd=cwd, config_path=args.config)
        alias = args.model
        if any([args.base_url, args.api_key, args.model_name, args.provider]):
            cfg.add_model(model_from_overrides(model_name=args.model_name, provider=args.provider,
                                               base_url=args.base_url, api_key=args.api_key),
                          make_default=True)
            alias = alias or "cli"
        model = cfg.get_model(alias)
        perms = PermissionManager(auto_approve=args.yolo or cfg.auto_approve,
                                  rules=cfg.permissions, injection_guard=cfg.injection_guard)
        return create_agent(model=model, config=cfg, cwd=cwd, permissions=perms,
                            skills=discover_skills(cwd), stream=True, verbose=False,
                            tracer=tracer_from_config(cfg))

    try:
        load_config(config_path=args.config)
    except ConfigError as exc:
        ui.error(str(exc))
        return 1
    server = ACPServer(make_agent, sys.stdin.readline,
                       lambda s: (sys.stdout.write(s), sys.stdout.flush()))
    server.serve()
    return 0
