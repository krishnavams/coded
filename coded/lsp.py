"""A small Language Server Protocol client for the `lsp` tool.

Speaks JSON-RPC over a language server's stdio (no dependency): one server
per language, started on first use with the project as its root and shared by
every agent in the process. Files are opened (or re-sent when they changed on
disk) before each request, so answers match what is on disk.

Config (`"lsp"`): `true` uses the servers found on PATH (`DEFAULT_SERVERS`);
a dict also sets commands per language, adds languages or turns one off:

    "lsp": {"python": ["pylsp"],
            "zig": {"command": ["zls"], "extensions": [".zig"]},
            "rust": false,
            "timeout": 60}
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urlparse

# (language, extensions, candidate commands in order of preference)
DEFAULT_SERVERS: List[Tuple[str, Tuple[str, ...], List[List[str]]]] = [
    ("python", (".py", ".pyi"), [["pyright-langserver", "--stdio"],
                                 ["basedpyright-langserver", "--stdio"],
                                 ["pylsp"], ["jedi-language-server"]]),
    ("typescript", (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"),
     [["typescript-language-server", "--stdio"]]),
    ("go", (".go",), [["gopls"]]),
    ("rust", (".rs",), [["rust-analyzer"]]),
    ("c", (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"), [["clangd"]]),
    ("java", (".java",), [["jdtls"]]),
    ("ruby", (".rb",), [["ruby-lsp"], ["solargraph", "stdio"]]),
    ("php", (".php",), [["intelephense", "--stdio"]]),
    ("lua", (".lua",), [["lua-language-server"]]),
    ("csharp", (".cs",), [["csharp-ls"]]),
]

_LANGUAGE_IDS = {
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "typescriptreact",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascriptreact",
    ".c": "c", ".h": "c", ".py": "python", ".pyi": "python",
}
DEFAULT_TIMEOUT = 30.0


class LSPError(Exception):
    """Something the model should hear about as text."""


def path_to_uri(path: str) -> str:
    p = Path(path).resolve().as_posix()
    if not p.startswith("/"):
        p = "/" + p  # Windows: C:/x → /C:/x
    return "file://" + quote(p, safe="/:")


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    path = unquote(parsed.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return str(Path(path))


def utf16_col(text: str, index: int) -> int:
    """A Python string index → the UTF-16 column LSP counts in."""
    return len(text[:index].encode("utf-16-le")) // 2


def py_col(text: str, col16: int) -> int:
    """A UTF-16 column → a Python string index."""
    units = 0
    for i, ch in enumerate(text):
        if units >= col16:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


# ---------------------------------------------------------------------------
# One server
# ---------------------------------------------------------------------------
@dataclass
class _Doc:
    version: int
    mtime: Tuple[int, int]  # (st_mtime_ns, st_size): a quick second edit still shows


class LSPClient:
    def __init__(self, argv: Sequence[str], root: str, language: str,
                 timeout: float = DEFAULT_TIMEOUT, init_options: Any = None):
        self.argv = list(argv)
        self.root = root
        self.language = language
        self.timeout = timeout
        self.init_options = init_options
        self.proc: Optional[subprocess.Popen] = None
        self.capabilities: Dict[str, Any] = {}
        self._ids = itertools.count(1)  # next() is atomic: parallel calls are safe
        self._write_lock = threading.Lock()
        self._doc_lock = threading.Lock()
        self._pending: Dict[int, Tuple[threading.Event, Dict[str, Any]]] = {}
        self._docs: Dict[str, _Doc] = {}
        self.diagnostics: Dict[str, Tuple[Optional[int], list]] = {}
        self._diag_event = threading.Condition()
        self.dead = ""

    # -- process and framing ---------------------------------------------------
    def start(self) -> None:
        from coded.shells import child_env, popen_kwargs

        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=self.root, env=child_env(),
                **popen_kwargs(new_group=True))
        except OSError as exc:
            raise LSPError(f"could not start {' '.join(self.argv)}: {exc}") from exc
        threading.Thread(target=self._read_loop, name=f"lsp-{self.language}",
                         daemon=True).start()
        root_uri = path_to_uri(self.root)
        result = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri, "rootPath": self.root,
            "workspaceFolders": [{"uri": root_uri, "name": Path(self.root).name or "root"}],
            "initializationOptions": self.init_options,
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": False},
                    "definition": {"linkSupport": True},
                    "typeDefinition": {"linkSupport": True},
                    "implementation": {"linkSupport": True},
                    "references": {},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "publishDiagnostics": {"versionSupport": True},
                    "diagnostic": {},
                },
                "workspace": {"symbol": {}, "workspaceFolders": True, "configuration": True},
                "window": {"workDoneProgress": False},
            },
        }, timeout=max(self.timeout, 60.0))
        self.capabilities = (result or {}).get("capabilities") or {}
        self.notify("initialized", {})

    def _send(self, msg: Dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            raise LSPError(f"the {self.language} language server is not running"
                           + (f" ({self.dead})" if self.dead else ""))
        body = json.dumps({"jsonrpc": "2.0", **msg}).encode("utf-8")
        with self._write_lock:
            try:
                self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self.proc.stdin.flush()
            except OSError as exc:
                raise LSPError(f"the {self.language} language server went away: {exc}") from exc

    def _read_loop(self) -> None:
        out = self.proc.stdout if self.proc else None
        try:
            while out is not None:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        raise EOFError
                    line = line.strip()
                    if not line:
                        break
                    name, _, value = line.decode("ascii", "replace").partition(":")
                    if name.lower() == "content-length":
                        length = int(value.strip())
                if length is None:
                    continue
                self._dispatch(json.loads(out.read(length).decode("utf-8", "replace")))
        except Exception as exc:  # noqa: BLE001 - EOF, bad frame: the server is gone
            self.dead = "it exited" if isinstance(exc, EOFError) else f"bad message: {exc}"
        for event, box in list(self._pending.values()):
            box["error"] = {"message": f"the language server stopped ({self.dead})"}
            event.set()

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        if "method" not in msg:  # a response to one of ours
            waiter = self._pending.pop(msg.get("id"), None)
            if waiter:
                waiter[1].update(msg)
                waiter[0].set()
            return
        method, params = msg["method"], msg.get("params") or {}
        if "id" in msg:  # the server asks us something: answer so it never waits
            if method == "workspace/configuration":
                result: Any = [None] * len(params.get("items") or [])
            elif method == "workspace/workspaceFolders":
                result = [{"uri": path_to_uri(self.root), "name": Path(self.root).name}]
            else:
                result = None
            try:
                self._send({"id": msg["id"], "result": result})
            except LSPError:
                pass
        elif method == "textDocument/publishDiagnostics":
            with self._diag_event:
                self.diagnostics[params.get("uri", "")] = (params.get("version"),
                                                           params.get("diagnostics") or [])
                self._diag_event.notify_all()

    def request(self, method: str, params: Any, timeout: Optional[float] = None) -> Any:
        rid = next(self._ids)
        event, box = threading.Event(), {}
        self._pending[rid] = (event, box)
        self._send({"id": rid, "method": method, "params": params})
        if not event.wait(timeout or self.timeout):
            self._pending.pop(rid, None)
            raise LSPError(f"the {self.language} language server did not answer {method} "
                           f"within {int(timeout or self.timeout)}s (it may still be indexing; "
                           "try again shortly)")
        if "error" in box:
            raise LSPError(f"{method}: {(box['error'] or {}).get('message', box['error'])}")
        return box.get("result")

    def notify(self, method: str, params: Any) -> None:
        self._send({"method": method, "params": params})

    # -- documents -----------------------------------------------------------------
    def sync(self, path: str) -> str:
        """Open the file, or send its new text if it changed on disk. Its URI."""
        uri = path_to_uri(path)
        st = os.stat(path)
        mtime = (st.st_mtime_ns, st.st_size)
        with self._doc_lock:
            doc = self._docs.get(uri)
            if doc is not None and doc.mtime == mtime:
                return uri
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            with self._diag_event:
                self.diagnostics.pop(uri, None)
            if doc is None:
                self._docs[uri] = _Doc(1, mtime)
                ext = Path(path).suffix.lower()
                self.notify("textDocument/didOpen", {"textDocument": {
                    "uri": uri, "languageId": _LANGUAGE_IDS.get(ext, self.language),
                    "version": 1, "text": text}})
            else:
                doc.version += 1
                doc.mtime = mtime
                self.notify("textDocument/didChange", {
                    "textDocument": {"uri": uri, "version": doc.version},
                    "contentChanges": [{"text": text}]})
        return uri

    def file_diagnostics(self, path: str, wait: float = 10.0) -> list:
        uri = self.sync(path)
        if self.capabilities.get("diagnosticProvider"):
            try:
                report = self.request("textDocument/diagnostic",
                                      {"textDocument": {"uri": uri}}) or {}
                if report.get("kind") == "full":
                    return report.get("items") or []
            except LSPError:
                pass  # fall back to what the server publishes
        deadline = time.monotonic() + wait
        with self._diag_event:
            while uri not in self.diagnostics:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LSPError("the language server reported no diagnostics for this file "
                                   f"within {int(wait)}s (it may still be analysing; try again)")
                self._diag_event.wait(left)
            return self.diagnostics[uri][1]

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.request("shutdown", None, timeout=2)
                self.notify("exit", None)
                self.proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        from coded.shells import kill_tree

        kill_tree(self.proc, timeout=2)


# ---------------------------------------------------------------------------
# Which server handles which file
# ---------------------------------------------------------------------------
@dataclass
class ServerSpec:
    language: str
    extensions: Tuple[str, ...]
    commands: List[List[str]]
    init_options: Any = None


def server_specs(settings: Any) -> Tuple[List[ServerSpec], float]:
    """The language table after the user's `lsp` settings, and the timeout."""
    settings = settings if isinstance(settings, dict) else {}
    specs = {lang: ServerSpec(lang, exts, [list(c) for c in cmds])
             for lang, exts, cmds in DEFAULT_SERVERS}
    for lang, value in settings.items():
        if lang == "timeout":
            continue
        if value is False or value is None:
            specs.pop(lang, None)
            continue
        if isinstance(value, (list, str)):
            value = {"command": value}
        if not isinstance(value, dict):
            continue
        spec = specs.get(lang) or ServerSpec(lang, (), [])
        command = value.get("command")
        if isinstance(command, str):
            command = command.split()
        if command:
            spec.commands = [list(command)]
        if value.get("extensions"):
            spec.extensions = tuple(e if e.startswith(".") else "." + e
                                    for e in value["extensions"])
        spec.init_options = value.get("initialization_options")
        specs[lang] = spec
    try:
        timeout = float(settings.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return list(specs.values()), timeout


def find_command(spec: ServerSpec, which: Callable[[str], Optional[str]] = shutil.which
                 ) -> Optional[List[str]]:
    for command in spec.commands:
        exe = which(command[0])
        if exe:
            return [exe] + command[1:]
    return None


class LSPManager:
    def __init__(self, root: str, settings: Any = True,
                 which: Callable[[str], Optional[str]] = shutil.which):
        self.root = str(Path(root).resolve())
        self.specs, self.timeout = server_specs(settings)
        self.which = which
        self.clients: Dict[str, LSPClient] = {}
        self.failed: Dict[str, str] = {}
        self._lock = threading.Lock()

    def spec_for(self, path: str) -> Optional[ServerSpec]:
        ext = Path(path).suffix.lower()
        return next((s for s in self.specs if ext in s.extensions), None)

    def client_for(self, path: str) -> LSPClient:
        spec = self.spec_for(path)
        if spec is None:
            known = ", ".join(sorted({e for s in self.specs for e in s.extensions}))
            raise LSPError(f"no language server is set up for {Path(path).suffix or 'this file'} "
                           f"files (known: {known}). Use grep/repo_map instead, or add one under "
                           "\"lsp\" in the config.")
        with self._lock:
            client = self.clients.get(spec.language)
            if client is not None and client.proc is not None and client.proc.poll() is None:
                return client
            if spec.language in self.failed:
                raise LSPError(self.failed[spec.language])
            argv = find_command(spec, self.which)
            if argv is None:
                names = " or ".join(c[0] for c in spec.commands) or "(none configured)"
                self.failed[spec.language] = (
                    f"no {spec.language} language server found on PATH ({names}). Install one, "
                    "or use grep/repo_map instead.")
                raise LSPError(self.failed[spec.language])
            client = LSPClient(argv, self.root, spec.language, self.timeout, spec.init_options)
            try:
                client.start()
            except LSPError as exc:
                client.stop()
                self.failed[spec.language] = f"the {spec.language} language server failed: {exc}"
                raise LSPError(self.failed[spec.language]) from exc
            self.clients[spec.language] = client
            return client

    def status(self) -> List[Tuple[str, str]]:
        """(language, state) for each language, for `coded doctor`."""
        out = []
        for spec in self.specs:
            argv = find_command(spec, self.which)
            state = ("running" if spec.language in self.clients
                     else (" ".join([Path(argv[0]).name] + argv[1:]) if argv else "not found"))
            out.append((spec.language, state))
        return out

    def stop(self) -> None:
        with self._lock:
            clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            client.stop()


_MANAGERS: Dict[Tuple[str, str], LSPManager] = {}
_MANAGERS_LOCK = threading.Lock()


def get_manager(root: str, settings: Any) -> LSPManager:
    """The process-wide manager for this project (sub-agents share its servers)."""
    key = (str(Path(root).resolve()), json.dumps(settings, sort_keys=True, default=str))
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = _MANAGERS[key] = LSPManager(root, settings)
        return manager


@atexit.register
def stop_all() -> None:
    with _MANAGERS_LOCK:
        managers = list(_MANAGERS.values())
        _MANAGERS.clear()
    for m in managers:
        m.stop()
