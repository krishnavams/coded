"""Command intelligence: what to suggest while you type a coded command.

One engine serves every input — the REPL prompt (through a prompt_toolkit
completer), the box shown while a turn runs, and the TUI. Nothing here draws or
reads keys; the front-ends ask `Engine.suggest(text, cursor)` and render the
`Suggestion`s.

What it knows, and where it comes from (all live, nothing is a canned list):

* **Commands**: the built-in slash commands (`repl.SLASH_COMMANDS`), custom
  commands (`.coded/commands`, plugins), skills (`/<skill>`).
* **Arguments**: each built-in's grammar (`GRAMMAR`, written next to what its
  handler accepts) names its subcommands, flags and value slots; the values are
  read from the running session — configured models and pools, output styles,
  saved sessions, this conversation's requests, running tasks and shells,
  memory notes, permission rules and the registered tools, files and folders.
* **Matching**: prefix, word-start/subsequence (fuzzy) and one or two typos,
  with the matched positions for highlighting.
* **Ranking**: match quality + how often and how recently you used it in this
  project (`UsageStore`) + what the session suggests doing next (`next_steps`:
  failed checks → /heal, edits → /review, running sub-agents → /tasks, …).
* **Corrections**: `correct("/reviw")` → ["/review"] for unknown commands.

A suggestion is only ever inserted into the input; running it takes the user's
own Enter.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

MAX_ITEMS = 12  # rows in a dropdown


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _damerau(a: str, b: str, limit: int) -> int:
    """Optimal-string-alignment distance, stopping early past `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: List[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def fuzzy(query: str, candidate: str) -> Optional[Tuple[float, Tuple[int, ...]]]:
    """How well `query` matches `candidate`: (score 0-100, matched positions),
    or None. Case-insensitive. Prefix > substring > letters in order (word
    starts count more) > a typo or two."""
    if not query:
        return 50.0, ()
    q, c = query.lower(), candidate.lower()
    if c.startswith(q):
        return 100.0 - min(len(c) - len(q), 30) * 0.3, tuple(range(len(q)))
    at = c.find(q)
    if at >= 0:
        return 80.0 - at * 0.5, tuple(range(at, at + len(q)))
    # Letters in order, preferring the start of words (/add-dir ← "ad", "adr").
    positions, j, bonus = [], 0, 0.0
    for i, ch in enumerate(c):
        if j < len(q) and ch == q[j]:
            positions.append(i)
            if i == 0 or not c[i - 1].isalnum():
                bonus += 4
            elif positions[:-1] and positions[-2] == i - 1:
                bonus += 2
            j += 1
    if j == len(q) and (len(q) >= 2 or positions[0] == 0):
        gaps = positions[-1] - positions[0] + 1 - len(q)
        return max(30.0, 65.0 + bonus - gaps * 2.0), tuple(positions)
    # A typo: compare with the candidate's prefix of about the same length.
    if len(q) >= 3:
        limit = 1 if len(q) < 6 else 2
        best = min(_damerau(q, c[:n], limit) for n in range(len(q) - 1, len(q) + 2))
        if best <= limit:
            same = tuple(i for i in range(min(len(q), len(c))) if q[i] == c[i])
            return 40.0 - best * 8, same
    return None


# ---------------------------------------------------------------------------
# What a suggestion is
# ---------------------------------------------------------------------------
@dataclass
class Suggestion:
    text: str                 # replaces input[start:end]
    start: int
    end: int
    display: str              # the row's label (a command, a value, a path)
    kind: str                 # command | subcommand | flag | value | path | recent | next
    description: str = ""
    usage: str = ""           # the command's syntax, e.g. "/model [name]"
    source: str = ""          # built-in | custom command | skill | plugin … | history
    match: Tuple[int, ...] = ()  # positions in `display` that matched
    score: float = 0.0
    reason: str = ""          # why it is suggested now (context)

    def apply(self, text: str) -> Tuple[str, int]:
        """The input after accepting this suggestion, and the new cursor."""
        new = text[:self.start] + self.text + text[self.end:]
        return new, self.start + len(self.text)


# ---------------------------------------------------------------------------
# Grammar of the built-in commands
# ---------------------------------------------------------------------------
@dataclass
class Slot:
    """One argument position. `provider` names a live source of values
    (`PROVIDERS`); `choices` are subcommands/keywords, each with a description
    and the slots that follow it."""

    name: str                                   # placeholder: "name", "path", …
    provider: Optional[str] = None
    choices: Tuple[Tuple[str, str, Tuple["Slot", ...]], ...] = ()
    optional: bool = True
    rest: bool = False                          # takes the rest of the line
    repeat: bool = False                        # may appear again (paths)

    @property
    def placeholder(self) -> str:
        inner = "|".join(c[0] for c in self.choices) if self.choices and not self.provider \
            else self.name
        return f"[{inner}]" if self.optional else f"<{inner}>"


@dataclass
class Flag:
    name: str
    description: str
    value: Optional[Slot] = None


@dataclass
class Spec:
    slots: Tuple[Slot, ...] = ()
    flags: Tuple[Flag, ...] = ()


def _onoff(what: str) -> Slot:
    return Slot("on|off", choices=(("on", f"Turn {what} on", ()), ("off", f"Turn {what} off", ())))


_RULE = Slot("rule", provider="rules_new", optional=False, rest=True)
GRAMMAR: Dict[str, Spec] = {
    # Written from each command's handler in repl.py (and the TUI's subset).
    "/model": Spec((Slot("name", provider="models"),)),
    "/route": Spec((_onoff("difficulty routing"),)),
    "/heal": Spec((_onoff("self-healing"),)),
    "/skill": Spec((Slot("name", provider="skills", optional=False),
                    Slot("task", rest=True))),
    "/review": Spec((Slot("target", provider="paths", repeat=True),),
                    (Flag("--adversarial", "Add a read-only adversarial critic"),)),
    "/redteam": Spec((Slot("target", provider="paths", repeat=True),)),
    "/refactor": Spec((Slot("path", provider="paths"),)),
    "/permissions": Spec((Slot("action", choices=(
        ("allow", "Run matching calls without asking", (_RULE,)),
        ("deny", "Always refuse matching calls", (_RULE,)),
        ("remove", "Remove a rule or an always-approval",
         (Slot("rule", provider="rules_existing", optional=False, rest=True),)),
        ("mode", "Switch the permission mode",
         (Slot("mode", provider="modes", optional=False),)),
        ("save", "Save this session's rules to the project config", ()),
    )),)),
    "/budget": Spec((Slot("dollars|off", provider="budget"),)),
    "/shells": Spec((Slot("action", choices=(
        ("kill", "Stop a background shell", (Slot("id", provider="shells", optional=False),)),
    )),)),
    "/tasks": Spec((Slot("id", provider="tasks", choices=(
        ("stop", "Stop a running sub-agent or shell",
         (Slot("id", provider="running", optional=False),)),
    )),)),
    "/btw": Spec((Slot("question", optional=False, rest=True),)),
    "/mission": Spec((Slot("goal", rest=True),),
                     (Flag("--budget", "Stop at this many dollars", Slot("dollars", optional=False)),
                      Flag("--resume", "Carry on with the last mission"),
                      Flag("--list", "List this project's missions"))),
    "/image": Spec((Slot("path", provider="images", optional=False, repeat=True),)),
    "/rewind": Spec((Slot("n", provider="turns"),),
                    (Flag("--keep-files", "Leave the files as they are"),)),
    "/timeline": Spec((Slot("n", provider="turns", choices=(
        ("restore", "Go back to before a request (with a preview)",
         (Slot("n", provider="turns", optional=False),)),
    )),)),
    "/style": Spec((Slot("name", provider="styles"),)),
    "/add-dir": Spec((Slot("path", provider="dirs"),)),
    "/export": Spec((Slot("path", provider="paths"),)),
    "/resume": Spec((Slot("n|id", provider="sessions"),)),
    "/memory": Spec((Slot("action", choices=(
        ("list", "Show the project's notes", ()),
        ("search", "Find notes by words", (Slot("words", optional=False, rest=True),)),
        ("add", "Add a note (\"kind: text\")",
         (Slot("note", provider="memory_kinds", optional=False, rest=True),)),
        ("forget", "Delete a note", (Slot("id", provider="memory", optional=False),)),
        ("clear", "Delete every note", ()),
    )),)),
    "/lab": Spec((Slot("action", choices=(("apply", "Review the proposals one by one", ()),)),)),
    "/plan": Spec((Slot("task", rest=True),)),
}


def usage_of(name: str, spec: Optional[Spec], hint: str = "") -> str:
    if spec is None:
        return f"{name} {hint}".strip()
    parts = [name] + [s.placeholder for s in spec.slots] + [
        f"[{f.name}{' ' + f.value.name if f.value else ''}]" for f in spec.flags]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The session the suggestions are for
# ---------------------------------------------------------------------------
@dataclass
class Command:
    name: str
    description: str
    source: str
    spec: Optional[Spec] = None
    hint: str = ""        # a custom command's argument-hint


@dataclass
class Context:
    """What the engine reads. Every field is optional, so a front-end (or a
    test) passes what it has; a missing source simply suggests nothing."""

    cwd: str = "."
    commands: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # name → (desc, source)
    custom: Dict[str, Any] = field(default_factory=dict)   # name → commands.Command
    skills: Dict[str, Any] = field(default_factory=dict)
    agent: Any = None
    config: Any = None
    sessions: Any = None   # sessions_store.SessionKeeper
    extra_dirs: Sequence[str] = ()


# ---------------------------------------------------------------------------
# Live values for argument slots
# ---------------------------------------------------------------------------
Value = Tuple[str, str]  # (value, description)


def _models(ctx: Context) -> List[Value]:
    cfg = ctx.config
    if cfg is None:
        return []
    current = getattr(getattr(ctx.agent, "model", None), "name", "")
    out = [(n, ("current · " if n == current else "") + f"model · {m.model}")
           for n, m in cfg.models.items()]
    out += [(n, f"pool · {', '.join(cfg.pool_members(n))}") for n in (cfg.pools or {})]
    return out


def _skills(ctx: Context) -> List[Value]:
    return [(n, getattr(s, "description", "") or "skill") for n, s in ctx.skills.items()]


def _styles(ctx: Context) -> List[Value]:
    from coded.styles import discover_styles

    out = [(n, getattr(s, "description", "") or "output style")
           for n, s in discover_styles(ctx.cwd).items()]
    return out + [("off", "Back to the default style")]


def _sessions(ctx: Context) -> List[Value]:
    keeper = ctx.sessions
    if keeper is None:
        return []
    out = []
    for n, m in enumerate(keeper.here()[:20], 1):
        preview = " ".join((m.preview or "(no request yet)").split())[:60]
        out.append((str(n), f"{preview} · {m.id}"))
    return out


def _turns(ctx: Context) -> List[Value]:
    agent = ctx.agent
    if agent is None or not hasattr(agent, "turn_history"):
        return []
    turns = agent.turn_history()
    return [(str(n), " ".join(t["text"].split())[:70])
            for n, t in reversed(list(enumerate(turns, 1)))]


def _tasks(ctx: Context, running_only: bool = False) -> List[Value]:
    agent = ctx.agent
    out: List[Value] = []
    if agent is None:
        return out
    tasks = getattr(agent, "tasks", None)
    for t in (tasks.all() if tasks is not None else []):
        if running_only and t.status != "running":
            continue
        out.append((t.id, f"{t.status} · {t.label}"[:70]))
    shells = getattr(agent, "shells", None)
    for sid, cmd, status in (shells.list() if shells is not None else []):
        if running_only and status != "running":
            continue
        out.append((sid, f"shell {status} · {cmd}"[:70]))
    return out


def _shells(ctx: Context) -> List[Value]:
    shells = getattr(ctx.agent, "shells", None)
    return [(sid, f"{status} · {cmd}"[:70]) for sid, cmd, status in
            (shells.list() if shells is not None else []) if status == "running"]


def _memory(ctx: Context) -> List[Value]:
    from coded.memory import MemoryStore

    return [(e.id, f"{e.kind} · {e.text}"[:70]) for e in MemoryStore.for_project(ctx.cwd).load()]


def _memory_kinds(ctx: Context) -> List[Value]:
    from coded.memory import KINDS

    return [(f"{k}:", f"a {k} note") for k in KINDS]


def _modes(ctx: Context) -> List[Value]:
    current = getattr(ctx.agent, "permission_mode", "")
    names = {"default": "Ask before risky actions",
             "accept-edits": "File edits run without asking",
             "plan": "Read-only research, then approve a plan"}
    return [(n, ("current · " if n == current else "") + d) for n, d in names.items()]


def _budget(ctx: Context) -> List[Value]:
    out = [("off", "No spending limit")]
    agent = ctx.agent
    if agent is not None:
        try:
            spent = agent.session.estimated_cost(agent.model)
            out.insert(0, (f"{max(1, math.ceil(spent * 2)):g}",
                           f"twice what this session has spent (${spent:.2f})"))
        except Exception:  # noqa: BLE001 - a guess, never required
            pass
    return out


def _rules_existing(ctx: Context) -> List[Value]:
    perms = getattr(ctx.agent, "permissions", None)
    if perms is None:
        return []
    return ([(r, "allow rule") for r in perms.allow_rules]
            + [(r, "deny rule") for r in perms.deny_rules]
            + [(k, "approved with \"always\"") for k in perms.always_allowed])


def _recent_shell_words(agent: Any, limit: int = 6) -> List[str]:
    """The first word of the shell commands the agent ran lately (newest first)."""
    words: List[str] = []
    for msg in reversed(getattr(getattr(agent, "session", None), "messages", []) or []):
        for tc in (msg.get("tool_calls") or []) if isinstance(msg, dict) else []:
            fn = tc.get("function") or {}
            if fn.get("name") != "bash":
                continue
            try:
                cmd = json.loads(fn.get("arguments") or "{}").get("command", "")
            except (ValueError, AttributeError):
                continue
            word = (cmd.split() or [""])[0]
            if word and word not in words:
                words.append(word)
                if len(words) >= limit:
                    return words
    return words


def _rules_new(ctx: Context) -> List[Value]:
    """Rules for the tools this session has, in their `tool(pattern)` form;
    bash rules for the commands the agent actually ran."""
    agent = ctx.agent
    registry = getattr(agent, "registry", None)
    names = registry.names() if registry is not None else []
    out = [(f"bash({w} *)", f"shell commands starting with {w}")
           for w in _recent_shell_words(agent)] if "bash" in names else []
    for name in names:
        out.append((name, f"every {name} call"))
        out.append((f"{name}(*)", f"{name} with any target"))
    return out


_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


class _Paths:
    """Files and folders under cwd (+ extra dirs), listed once and cached."""

    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[float, List[str]]] = {}
        self._lock = threading.Lock()

    def get(self, cwd: str, ttl: float = 15.0) -> List[str]:
        with self._lock:
            hit = self._cache.get(cwd)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        from coded.mentions import list_paths

        ignore = None
        try:
            from coded.ignore import rules_for

            rules = rules_for(cwd)
            if rules:
                ignore = lambda p: rules.ignored_rel(p.rstrip("/"), p.endswith("/"))  # noqa: E731
        except Exception:  # noqa: BLE001 - no ignore file support: list everything
            ignore = None
        paths = list_paths(cwd, ignore=ignore)
        with self._lock:
            self._cache[cwd] = (time.monotonic(), paths)
        return paths

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


PATHS = _Paths()


def _paths(ctx: Context) -> List[Value]:
    return [(p, "folder" if p.endswith("/") else "file") for p in PATHS.get(ctx.cwd)]


def _dirs(ctx: Context) -> List[Value]:
    return [(p, "folder") for p in PATHS.get(ctx.cwd) if p.endswith("/")]


def _images(ctx: Context) -> List[Value]:
    return [(p, "folder" if p.endswith("/") else "image") for p in PATHS.get(ctx.cwd)
            if p.endswith("/") or p.lower().endswith(_IMAGE_EXT)]


PROVIDERS: Dict[str, Callable[[Context], List[Value]]] = {
    "models": _models, "skills": _skills, "styles": _styles, "sessions": _sessions,
    "turns": _turns, "tasks": _tasks, "running": lambda c: _tasks(c, running_only=True),
    "shells": _shells, "memory": _memory, "memory_kinds": _memory_kinds, "modes": _modes,
    "budget": _budget, "rules_existing": _rules_existing, "rules_new": _rules_new,
    "paths": _paths, "dirs": _dirs, "images": _images,
}
# How long a provider's answer is reused (seconds): typing a word asks many times.
_TTL = {"sessions": 5.0, "memory": 3.0, "styles": 10.0, "paths": 15.0, "dirs": 15.0,
        "images": 15.0}
_PATH_PROVIDERS = {"paths", "dirs", "images"}


# ---------------------------------------------------------------------------
# Learning: which commands this project uses
# ---------------------------------------------------------------------------
def usage_path() -> Path:
    env = os.environ.get("CODED_USAGE_STORE")
    if env:
        return Path(env).expanduser()
    from coded.config import user_config_path

    return user_config_path().parent / "command_usage.json"


class UsageStore:
    """Per project: each command line run (count, last time). Bounded, written
    atomically, re-read before each write so two sessions do not clobber."""

    MAX_LINES = 200
    MAX_PROJECTS = 50

    def __init__(self, cwd: str, path: Optional[Path] = None) -> None:
        self.key = os.path.realpath(cwd)
        self.path = path or usage_path()
        self._lock = threading.Lock()
        self._data: Optional[Dict[str, List[float]]] = None

    def _read_all(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def lines(self) -> Dict[str, List[float]]:
        with self._lock:
            if self._data is None:
                raw = self._read_all().get(self.key, {})
                self._data = {k: v for k, v in raw.items()
                              if isinstance(v, list) and len(v) == 2} if isinstance(raw, dict) else {}
            return self._data

    def record(self, line: str) -> None:
        line = " ".join(line.split())
        if not line.startswith("/") or len(line) > 200:
            return
        with self._lock:
            data = self._read_all()
            mine = data.get(self.key) if isinstance(data.get(self.key), dict) else {}
            count, _ = mine.get(line, [0, 0])
            mine[line] = [count + 1, time.time()]
            if len(mine) > self.MAX_LINES:
                keep = sorted(mine.items(), key=lambda kv: kv[1][1], reverse=True)
                mine = dict(keep[:self.MAX_LINES])
            data[self.key] = mine
            if len(data) > self.MAX_PROJECTS:
                newest = sorted(data.items(), key=lambda kv: max(
                    (v[1] for v in kv[1].values()), default=0), reverse=True)
                data = dict(newest[:self.MAX_PROJECTS])
            self._data = mine
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass

    def boost(self, line: str) -> float:
        """0-15: used often and lately ranks higher (a week's half-life)."""
        hit = self.lines().get(line)
        if not hit:
            return 0.0
        count, last = hit
        age_days = max(0.0, (time.time() - last) / 86400)
        return min(15.0, 4.0 * math.log1p(count) + 6.0 * 0.5 ** (age_days / 7))

    def command_boost(self, name: str) -> float:
        """A command's boost from every line that used it."""
        best = 0.0
        for line in self.lines():
            if line == name or line.startswith(name + " "):
                best = max(best, self.boost(line))
        return best


# ---------------------------------------------------------------------------
# Context: what to do next
# ---------------------------------------------------------------------------
def _last_turn_events(agent: Any) -> List[Dict[str, Any]]:
    events = getattr(agent, "events", None)
    if events is None:
        return []
    items = events.snapshot()
    for i in range(len(items) - 1, -1, -1):
        if items[i].get("kind") == "turn":
            return items[i:]
    return items


def next_steps(ctx: Context) -> List[Tuple[str, str]]:
    """(command, why) the session points at right now, most pressing first.
    Read from the agent's state and the last request's event log."""
    agent = ctx.agent
    if agent is None:
        return []
    out: List[Tuple[str, str]] = []

    def add(cmd: str, why: str) -> None:
        if cmd not in (c for c, _ in out):
            out.append((cmd, why))

    active = set(getattr(agent, "active_tools", ()) or ())
    tasks = getattr(agent, "tasks", None)
    running = tasks.running() if tasks is not None else []
    if running or "task" in active:
        n = len(running) or 1
        add("/tasks", f"{n} sub-agent{'s' if n > 1 else ''} running (Ctrl+B sends one to the background)")
    shells = getattr(agent, "shells", None)
    if shells is not None and any(s == "running" for _, _, s in shells.list()):
        add("/shells", "a background shell is running")
    if active:
        add("/btw", f"ask something while {', '.join(sorted(active))} runs")
        add("/dashboard", "watch the tools, tasks and cost live")

    events = _last_turn_events(agent)
    verify = [e for e in events if e.get("kind") == "verify"]
    if verify and verify[-1].get("result") in ("fail", "stall"):
        why = "the checks are stuck failing" if verify[-1]["result"] == "stall" else "the checks failed"
        if not getattr(agent, "self_heal", False):
            add("/heal", f"{why}: fix failures automatically after each change")
        add("/timeline", f"{why}: see what each step did")
    tools = [e for e in events if e.get("kind") == "tool"]
    errors = [e for e in tools if e.get("error") and not e.get("denied")]
    if errors:
        add("/dashboard", f"{len(errors)} tool error{'s' if len(errors) > 1 else ''} in the last request")
    if any(e.get("denied") and e.get("reason") == "user" for e in tools):
        add("/permissions", "you denied a call: make a rule for it")
    if any(e.get("kind") == "max_turns" for e in events):
        add("/timeline", "the last request hit its step limit")
    from coded.permissions import EDIT_KEYS

    if any(e.get("name") in EDIT_KEYS and not e.get("error") for e in tools):
        add("/review", "files changed: review them")
        add("/undo", "files changed: take the edits back")
    session = getattr(agent, "session", None)
    model = getattr(agent, "_sized_model", None) or getattr(agent, "model", None)
    window = getattr(model, "context_window", 0) or 0
    if session is not None and window:
        from coded.compaction import current_input_tokens

        if current_input_tokens(session) > 0.7 * window:
            add("/compact", "the context window is over 70% full")
            add("/context", "see what fills the context window")
    cfg = ctx.config
    if cfg is not None and getattr(cfg, "max_cost", None) and session is not None:
        try:
            if session.estimated_cost(agent.model) > 0.8 * cfg.max_cost:
                add("/budget", "over 80% of the spending limit")
        except Exception:  # noqa: BLE001
            pass
    if getattr(agent, "permission_mode", "") == "plan":
        add("/plan", "plan mode is on: leave it")
    todos = getattr(getattr(agent, "todos", None), "items", None) or []
    if any(t.get("status") != "completed" for t in todos):
        add("/todos", "the task list has open items")
    if session is not None and getattr(session, "turns", 0) == 0 and not any(
            (Path(ctx.cwd) / f).exists() for f in ("CODED.md", "AGENTS.md", "CLAUDE.md")):
        add("/init", "no project guide yet: write CODED.md")
    return [(c, w) for c, w in out if c in ctx.commands]


# ---------------------------------------------------------------------------
# Splitting the input
# ---------------------------------------------------------------------------
@dataclass
class Token:
    start: int
    end: int
    value: str      # unquoted
    quoted: str = ""  # the quote character it started with, if any


def tokenize(line: str) -> List[Token]:
    """Words of a command line; quotes group spaces ("my notes.md"), a
    backslash escapes the next character outside single quotes."""
    out: List[Token] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            break
        start, buf, quote = i, [], ""
        if line[i] in "\"'":
            quote = line[i]
        while i < n and (quote or not line[i].isspace()):
            ch = line[i]
            if quote and ch == quote and i != start:
                i += 1
                quote = ""
                break
            if ch == "\\" and quote != "'" and i + 1 < n:
                buf.append(line[i + 1])
                i += 2
                continue
            if not (ch in "\"'" and i == start):
                buf.append(ch)
            i += 1
        while i < n and not line[i].isspace():  # text glued after a closing quote
            buf.append(line[i])
            i += 1
        out.append(Token(start, i, "".join(buf), line[start] if line[start] in "\"'" else ""))
    return out


def quote_if_needed(value: str) -> str:
    if value and not any(c.isspace() or c in "\"'\\" for c in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class Engine:
    """Suggestions for one input at a cursor. Thread-safe and cached: the
    front-ends call it on every keystroke (the REPL from a worker thread)."""

    def __init__(self, ctx: Context, usage: Optional[UsageStore] = None) -> None:
        self.ctx = ctx
        self.usage = usage if usage is not None else UsageStore(ctx.cwd)
        self._lock = threading.Lock()
        self._values: Dict[str, Tuple[float, List[Value]]] = {}
        self._registry: Optional[Dict[str, Command]] = None
        self._last: Optional[Tuple[str, int, List[Suggestion]]] = None

    # -- registry --------------------------------------------------------------
    def registry(self) -> Dict[str, Command]:
        with self._lock:
            if self._registry is None:
                self._registry = self._build_registry()
            return self._registry

    def _build_registry(self) -> Dict[str, Command]:
        reg: Dict[str, Command] = {}
        for name, (desc, source) in self.ctx.commands.items():
            reg[name] = Command(name, desc, source, GRAMMAR.get(name))
        for name, cmd in self.ctx.custom.items():
            key = f"/{name}"
            if key in reg:
                continue
            plugin = name.split(":", 1)[0] if ":" in name else ""
            reg[key] = Command(key, getattr(cmd, "description", "") or "custom command",
                               f"plugin {plugin}" if plugin else "custom command",
                               hint=getattr(cmd, "argument_hint", "") or "")
        for name, skill in self.ctx.skills.items():
            key = f"/{name}"
            if key not in reg:
                reg[key] = Command(key, getattr(skill, "description", "") or "skill", "skill",
                                   Spec((Slot("task", rest=True),)))
        return reg

    def invalidate(self) -> None:
        """Forget cached commands and values (after /clear, a turn, new files)."""
        with self._lock:
            self._registry = None
            self._values.clear()
            self._last = None
        PATHS.clear()

    def values(self, provider: str) -> List[Value]:
        ttl = _TTL.get(provider, 1.0)
        with self._lock:
            hit = self._values.get(provider)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        fn = PROVIDERS.get(provider)
        try:
            vals = list(fn(self.ctx)) if fn is not None else []
        except Exception:  # noqa: BLE001 - an unavailable source suggests nothing
            vals = []
        with self._lock:
            self._values[provider] = (time.monotonic(), vals)
        return vals

    # -- public ----------------------------------------------------------------
    def suggest(self, text: str, cursor: Optional[int] = None,
                limit: int = MAX_ITEMS) -> List[Suggestion]:
        cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))
        last = self._last
        if last is not None and last[0] == text and last[1] == cursor:
            return last[2]
        try:
            items = self._suggest(text, cursor)
        except Exception:  # noqa: BLE001 - suggestions must never break typing
            items = []
        items = _dedupe(items)[:limit]
        self._last = (text, cursor, items)
        return items

    def correct(self, word: str, limit: int = 3) -> List[str]:
        """Known commands close to an unknown one (typos, letters in order)."""
        if not word.startswith("/"):
            return []
        scored = []
        for name in self.registry():
            m = fuzzy(word, name)
            if m is not None and m[0] >= 30 and name != word:
                scored.append((-m[0], name))
        return [n for _, n in sorted(scored)[:limit]]

    def record(self, line: str) -> None:
        """A command line the user ran: learn from it."""
        if line.split()[:1] and line.split()[0] in self.registry():
            self.usage.record(line)
            with self._lock:
                self._last = None

    # -- internals -------------------------------------------------------------
    def _suggest(self, text: str, cursor: int) -> List[Suggestion]:
        before = text[:cursor]
        line_start = before.rfind("\n") + 1
        line = text[line_start:]
        rel = cursor - line_start
        tokens = tokenize(line[:rel])
        current = tokens[-1] if tokens and tokens[-1].end == rel else None
        # @path anywhere in a message.
        if current is not None and current.value.startswith("@"):
            return self._mentions(current, line_start)
        if line_start != 0 or not line.startswith("/"):
            return []
        if current is not None and current is tokens[0]:
            return self._commands(current.value, current.end, line[:rel])
        cmd = self.registry().get(tokens[0].value)
        if cmd is None:
            return []
        args = tokens[1:-1] if current is not None else tokens[1:]
        word = current.value if current is not None else ""
        start = current.start if current is not None else rel
        return [self._shift(s, line_start) for s in
                self._arguments(cmd, args, word, start, rel, bool(current and current.quoted))]

    @staticmethod
    def _shift(s: Suggestion, by: int) -> Suggestion:
        s.start += by
        s.end += by
        return s

    def _commands(self, word: str, end: int, typed: str) -> List[Suggestion]:
        out: List[Suggestion] = []
        nexts = dict(next_steps(self.ctx)) if word == "/" or len(word) <= 2 else {}
        bare = word == "/"
        order_of: Dict[str, int] = {}
        for order, (name, cmd) in enumerate(self.registry().items()):
            m = fuzzy(word, name)
            if m is None:
                continue
            # A bare "/" keeps the registry's order (help first, …) below what
            # the session and your habits put on top.
            score = (100.0 - order * 0.01 if bare else m[0]) + self.usage.command_boost(name)
            reason = nexts.get(name, "")
            if reason:
                score += 30
            out.append(Suggestion(
                name + " ", 0, end, name, "next" if reason else "command", cmd.description,
                usage_of(name, cmd.spec, cmd.hint), cmd.source, m[1], score, reason))
            order_of[name] = order
        # Whole lines used before ("/model fast"), when they match what is typed.
        for line, _ in self.usage.lines().items():
            if " " not in line or not line.startswith(word.rstrip()):
                continue
            m = fuzzy(word, line)
            if m is None:
                continue
            out.append(Suggestion(line + " ", 0, end, line, "recent", "used before",
                                  "", "history", m[1], m[0] - 5 + self.usage.boost(line)))
        out.sort(key=lambda s: (-s.score, 0 if bare else len(s.display),
                                order_of.get(s.display, len(order_of))))
        return out

    def _arguments(self, cmd: Command, args: List[Token], word: str, start: int, end: int,
                   quoted: bool) -> List[Suggestion]:
        spec = cmd.spec
        if spec is None:
            return []
        slots = list(spec.slots)
        used_flags = set()
        pos = 0
        pending_flag: Optional[Flag] = None
        for tok in args:
            if pending_flag is not None:
                pending_flag = None
                continue
            flag = next((f for f in spec.flags if f.name == tok.value), None)
            if flag is not None:
                used_flags.add(flag.name)
                pending_flag = flag if flag.value is not None else None
                continue
            if pos >= len(slots):
                return []  # more words than the command takes
            slot = slots[pos]
            if slot.rest:
                return []  # free text from here on
            choice = next((c for c in slot.choices if c[0] == tok.value), None)
            if choice is not None:
                slots = slots[:pos] + list(choice[2])
                continue
            if not slot.repeat:
                pos += 1
        out: List[Suggestion] = []
        if pending_flag is not None:
            return self._values_for(pending_flag.value, word, start, end, cmd, quoted)
        if pos < len(slots):
            out += self._values_for(slots[pos], word, start, end, cmd, quoted)
        if not word or word.startswith("-"):
            for f in spec.flags:
                if f.name in used_flags:
                    continue
                m = fuzzy(word, f.name)
                if m is not None:
                    out.append(Suggestion(f.name + " ", start, end, f.name, "flag",
                                          f.description, usage_of(cmd.name, spec), "built-in",
                                          m[1], m[0] - 10))
        out.sort(key=lambda s: -s.score)  # stable: ties keep the source's own order
        return out

    def _values_for(self, slot: Slot, word: str, start: int, end: int, cmd: Command,
                    quoted: bool) -> List[Suggestion]:
        out: List[Suggestion] = []
        usage = usage_of(cmd.name, cmd.spec)
        for name, desc, nxt in slot.choices:
            m = fuzzy(word, name)
            if m is not None:
                out.append(Suggestion(name + " ", start, end, name, "subcommand", desc, usage,
                                      cmd.source, m[1], m[0] + 5 + self.usage.boost(
                                          f"{cmd.name} {name}")))
        if slot.provider is None:
            return out
        is_path = slot.provider in _PATH_PROVIDERS
        values = self.values(slot.provider)
        if is_path:
            from coded.mentions import rank_paths

            ranked = rank_paths(word, [v for v, _ in values], limit=40)
            kinds = dict(values)
            for p in ranked:
                m = fuzzy(word, p) or (20.0, ())
                text = quote_if_needed(p) if (quoted or " " in p) else p
                out.append(Suggestion(text + ("" if p.endswith("/") else " "), start, end, p,
                                      "path", kinds.get(p, ""), usage, cmd.source, m[1],
                                      m[0] - len(p) * 0.01))
            return out
        for value, desc in values:
            m = fuzzy(word, value) or (fuzzy(word, desc) if word and len(word) > 2 else None)
            if m is None:
                continue
            positions = m[1] if fuzzy(word, value) else ()
            boost = self.usage.boost(f"{cmd.name} {value}")
            out.append(Suggestion(quote_if_needed(value) + ("" if value.endswith(":") else " "),
                                  start, end, value, "value", desc, usage, cmd.source,
                                  positions, m[0] + boost))
        return out

    def _mentions(self, tok: Token, line_start: int) -> List[Suggestion]:
        from coded.mentions import rank_paths

        query = tok.value[1:]
        out = []
        for p in rank_paths(query, PATHS.get(self.ctx.cwd), limit=MAX_ITEMS):
            shown = f'@"{p}"' if " " in p else "@" + p
            m = fuzzy(query, p) or (20.0, ())
            out.append(Suggestion(shown, line_start + tok.start, line_start + tok.end, p,
                                  "path", "folder" if p.endswith("/") else "file", "@path",
                                  "mention", m[1], m[0]))
        return out


def _dedupe(items: Iterable[Suggestion]) -> List[Suggestion]:
    seen, out = set(), []
    for s in items:
        key = (s.text.rstrip(), s.start)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# A dropdown's state, for inputs that draw their own (the mid-turn box)
# ---------------------------------------------------------------------------
class MenuState:
    """The dropdown under an input that is not prompt_toolkit's.

    Reads the input through `get()` → (text, cursor) — a `LineEditor` snapshot —
    so it always matches what is on screen. `index` is None until ↑/↓ picks a
    row: Tab takes the picked row or the first; Enter only takes a picked one
    (otherwise Enter sends the line). Esc hides the menu until the text changes.
    Thread-safe: the key reader moves it, the dock draws it.
    """

    def __init__(self, engine: Engine, limit: int = 8) -> None:
        self.engine = engine
        self.limit = limit
        self.index: Optional[int] = None
        self._key: Optional[Tuple[str, int]] = None
        self._dismissed: Optional[str] = None
        self._lock = threading.RLock()

    def items(self, text: str, cursor: int) -> List[Suggestion]:
        with self._lock:
            if self._key != (text, cursor):
                self._key = (text, cursor)
                self.index = None
            if self._dismissed is not None:
                if self._dismissed == text:
                    return []
                self._dismissed = None
            items = [s for s in self.engine.suggest(text, cursor, self.limit) if s.end == cursor]
            # A lone exact match is already typed: nothing to offer.
            if len(items) == 1 and text[items[0].start:cursor] == items[0].text.rstrip():
                return []
            return items

    def is_open(self, text: str, cursor: int) -> bool:
        return bool(self.items(text, cursor))

    def move(self, text: str, cursor: int, step: int) -> None:
        with self._lock:
            items = self.items(text, cursor)
            if not items:
                return
            if self.index is None:
                self.index = 0 if step > 0 else len(items) - 1
            else:
                self.index = (self.index + step) % len(items)

    def picked(self, text: str, cursor: int) -> Optional[Suggestion]:
        with self._lock:
            items = self.items(text, cursor)
            if self.index is None or not items:
                return None
            return items[min(self.index, len(items) - 1)]

    def accept(self, editor, explicit: bool = False) -> Optional[Suggestion]:
        """Put the picked row (or, unless `explicit`, the first) into `editor`."""
        snap = editor.snapshot()
        with self._lock:
            items = self.items(snap.text, snap.cursor)
            if not items:
                return None
            chosen = self.picked(snap.text, snap.cursor)
            if chosen is None:
                if explicit:
                    return None
                chosen = items[0]
            new, cursor = chosen.apply(snap.text)
            editor.set_text(new, cursor)
            self.index = None
            return chosen

    def dismiss(self, text: str) -> None:
        with self._lock:
            self._dismissed = text
            self.index = None
