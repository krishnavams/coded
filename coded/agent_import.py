"""`coded import`: copy MCP servers configured for other coding agents.

Another agent's add-on tools come from MCP servers, and most agents keep
those in a JSON (or TOML) file. This finds them, converts each to coded's
`mcp_servers` entry, shows every command verbatim and copies the chosen
ones into **one** coded config file after a confirmation, the same way the
config wizard edits a file: the raw JSON, never a merged Config.

Rules:
- coded's MCP client speaks stdio only, so URL (http/sse) servers are listed
  as skipped, with the reason.
- A name the target file already defines is never overwritten.
- Literal `env` values are usually secrets: they may go to the user file
  (written with mode 600) but never to the project file, which is shared.
  `${VAR}` references are fine anywhere (coded expands them at start).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from coded.config import ConfigError

AGENTS = ("claude", "claude-desktop", "cursor", "vscode", "gemini", "codex", "windsurf")

_REF = re.compile(r"^\$\{[^}]+\}$")


@dataclass
class Found:
    """One MCP server found in another agent's config."""

    name: str
    agent: str
    source: str  # the file it came from
    entry: Dict[str, Any] = field(default_factory=dict)  # coded's mcp_servers form
    skip: str = ""  # why it cannot be imported; empty = importable
    notes: List[str] = field(default_factory=list)

    @property
    def command_line(self) -> str:
        return " ".join([self.entry.get("command", "")] + list(self.entry.get("args", [])))

    @property
    def literal_env(self) -> List[str]:
        return [k for k, v in (self.entry.get("env") or {}).items()
                if not _REF.match(str(v))]


# ---------------------------------------------------------------------------
# Where each agent keeps its servers
# ---------------------------------------------------------------------------
def _desktop_config(home: Path, platform: str, env: Dict[str, str]) -> Path:
    if platform == "darwin":
        return home / "Library/Application Support/Claude/claude_desktop_config.json"
    if platform.startswith("win"):
        return Path(env.get("APPDATA") or home / "AppData/Roaming") / "Claude/claude_desktop_config.json"
    return home / ".config/Claude/claude_desktop_config.json"


def candidate_files(cwd: str, home: Optional[Path] = None, platform: str = sys.platform,
                    env: Optional[Dict[str, str]] = None) -> List[Tuple[str, Path, str]]:
    """(agent, path, format) for every place an agent may keep MCP servers."""
    home = Path(home or Path.home())
    env = dict(os.environ if env is None else env)
    proj = Path(cwd)
    return [
        ("claude", home / ".claude.json", "claude-json"),
        ("claude", proj / ".mcp.json", "mcpServers"),
        ("claude-desktop", _desktop_config(home, platform, env), "mcpServers"),
        ("cursor", home / ".cursor/mcp.json", "mcpServers"),
        ("cursor", proj / ".cursor/mcp.json", "mcpServers"),
        ("vscode", proj / ".vscode/mcp.json", "servers"),
        ("gemini", home / ".gemini/settings.json", "mcpServers"),
        ("gemini", proj / ".gemini/settings.json", "mcpServers"),
        ("codex", Path(env.get("CODEX_HOME") or home / ".codex") / "config.toml", "toml"),
        ("windsurf", home / ".codeium/windsurf/mcp_config.json", "mcpServers"),
    ]


def _read(path: Path, fmt: str) -> Tuple[Dict[str, Any], str]:
    """The file's server map, or ({}, why) when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"could not read: {exc}"
    try:
        if fmt == "toml":
            try:
                import tomllib  # Python 3.11+
            except ModuleNotFoundError:
                try:
                    import tomli as tomllib  # type: ignore[no-redef]
                except ModuleNotFoundError:
                    return {}, "reading TOML needs Python 3.11+ (or `pip install tomli`)"
            data = tomllib.loads(text)
        else:
            data = json.loads(text or "{}")
    except Exception as exc:  # noqa: BLE001 - JSON or TOML syntax
        return {}, f"not valid {'TOML' if fmt == 'toml' else 'JSON'}: {exc}"
    if not isinstance(data, dict):
        return {}, "does not hold an object"
    key = {"toml": "mcp_servers", "servers": "servers"}.get(fmt, "mcpServers")
    servers = data.get(key)
    if fmt == "mcpServers" and servers is None and all(isinstance(v, dict) for v in data.values()):
        servers = data if any("command" in v or "url" in v for v in data.values()) else None
    return (servers if isinstance(servers, dict) else {}), ""


def _claude_json(path: Path, cwd: str) -> Tuple[Dict[str, Any], str]:
    """~/.claude.json: user-scope servers, plus this project's local ones."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"could not read: {exc}"
    servers = dict(data.get("mcpServers") or {})
    for proj, pdata in (data.get("projects") or {}).items():
        try:
            same = Path(proj).resolve() == Path(cwd).resolve()
        except OSError:
            same = False
        if same and isinstance(pdata, dict):
            servers.update(pdata.get("mcpServers") or {})
    return servers, ""


_VSCODE_REF = re.compile(r"\$\{(?:env:)?(\w+)\}")


def convert(name: str, raw: Any, agent: str, source: str) -> Found:
    """One server in another agent's format → coded's `mcp_servers` entry."""
    found = Found(name=name, agent=agent, source=source)
    if not isinstance(raw, dict):
        found.skip = "not an object"
        return found
    kind = str(raw.get("type") or raw.get("transport") or "").lower()
    url = raw.get("url") or raw.get("httpUrl") or raw.get("serverUrl")
    if url or kind in ("http", "sse", "streamable-http", "streamable_http"):
        found.skip = f"a URL server ({url or kind}); coded's MCP client runs stdio servers only"
        return found
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        found.skip = "no command to run"
        return found
    args = raw.get("args") or []
    if not isinstance(args, list):
        found.skip = "`args` is not a list"
        return found
    entry: Dict[str, Any] = {"command": command, "args": [str(a) for a in args]}
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        found.skip = "`env` is not an object"
        return found
    if env:
        entry["env"] = {str(k): str(v) for k, v in env.items()}
    text = json.dumps(entry)
    if "${input:" in text:
        found.skip = "asks VS Code for an input value (${input:…}); set it up in coded by hand"
        return found
    if agent == "vscode":  # ${env:VAR} → ${VAR}, the form coded expands
        entry = json.loads(_VSCODE_REF.sub(lambda m: "${" + m.group(1) + "}", text))
    if raw.get("disabled") is True or raw.get("enabled") is False:
        entry["enabled"] = False
        found.notes.append("disabled there, imported disabled")
    for key in ("cwd", "envFile", "env_file"):
        if raw.get(key):
            found.notes.append(f"`{key}` is not supported by coded and was left out")
    found.entry = entry
    return found


def discover(cwd: str, agents: Optional[List[str]] = None, *, home: Optional[Path] = None,
             platform: str = sys.platform, env: Optional[Dict[str, str]] = None
             ) -> Tuple[List[Found], List[str]]:
    """Every server found (in file order) and notes about unreadable files."""
    found: List[Found] = []
    problems: List[str] = []
    seen: Dict[str, Found] = {}
    for agent, path, fmt in candidate_files(cwd, home, platform, env):
        if agents and agent not in agents:
            continue
        if not path.is_file():
            continue
        servers, why = _claude_json(path, cwd) if fmt == "claude-json" else _read(path, fmt)
        if why:
            problems.append(f"{agent}: {path}: {why}")
            continue
        for name, raw in servers.items():
            item = convert(str(name), raw, agent, str(path))
            first = seen.get(item.name)
            if first is not None:
                if first.entry != item.entry:
                    item.skip = f"the name is taken by the {first.agent} server of the same name"
                else:
                    continue  # the same server set up in two agents
            else:
                seen[item.name] = item
            found.append(item)
    return found, problems


# ---------------------------------------------------------------------------
# Choosing and writing
# ---------------------------------------------------------------------------
def plan(found: List[Found], existing: Dict[str, Any], *, project: bool) -> None:
    """Mark what cannot go into this file (in place)."""
    for item in found:
        if item.skip:
            continue
        if item.name in existing:
            item.skip = "already in this coded config (left unchanged)"
        elif project and item.literal_env:
            item.skip = ("its env holds literal values (" + ", ".join(item.literal_env)
                         + "); the project config is shared, import it without --project")


def describe(item: Found) -> List[str]:
    lines = [f"{item.name}  ({item.agent}: {item.source})"]
    if item.skip:
        lines.append(f"    skipped: {item.skip}")
        return lines
    lines.append(f"    runs: {item.command_line}")
    for k, v in (item.entry.get("env") or {}).items():
        shown = v if _REF.match(v) else "<value copied>"
        lines.append(f"    env:  {k}={shown}")
    for note in item.notes:
        lines.append(f"    note: {note}")
    return lines


def apply(data: Dict[str, Any], chosen: List[Found]) -> Dict[str, Any]:
    """A copy of the file's JSON with the chosen servers added."""
    out = dict(data)
    servers = dict(out.get("mcp_servers") or {})
    for item in chosen:
        servers.setdefault(item.name, item.entry)
    out["mcp_servers"] = servers
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coded import",
        description="Copy MCP servers (the tools) set up for other coding agents "
                    "into coded's config. Shows each command and asks first.")
    p.add_argument("--from", dest="agents", default="",
                   help="Only these agents, comma-separated: " + ", ".join(AGENTS))
    p.add_argument("--only", default="", help="Only these server names, comma-separated.")
    p.add_argument("--list", action="store_true", help="Show what was found; change nothing.")
    p.add_argument("--project", action="store_true",
                   help="Write ./.coded/config.json instead of the user config.")
    p.add_argument("--config", default=None, help="Write this config file instead.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Import everything importable without asking (after review).")
    return p


def main(argv: List[str], *, cwd: Optional[str] = None, home: Optional[Path] = None,
         prompter=None, interactive: Optional[bool] = None,
         out: Optional[Callable[[str], None]] = None) -> int:
    from coded import config_wizard as cw

    args = build_parser().parse_args(argv)
    cwd = cwd or os.getcwd()
    if out is None:
        from rich.markup import escape

        from coded import ui

        out = lambda s: ui.console.print(escape(s))  # noqa: E731
    agents = [a.strip() for a in args.agents.split(",") if a.strip()] or None
    unknown = [a for a in agents or [] if a not in AGENTS]
    if unknown:
        out(f"Unknown agent(s): {', '.join(unknown)}. Choose from: {', '.join(AGENTS)}.")
        return 2

    found, problems = discover(cwd, agents, home=home)
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        found = [f for f in found if f.name in wanted]
    for p in problems:
        out(f"! {p}")
    if not found:
        out("No MCP servers found in other agents' configs"
            + (f" ({', '.join(agents)})" if agents else "") + ".")
        return 0

    try:
        path = cw.target_path(project=args.project, config_path=args.config, cwd=cwd)
        data = cw.read_file(path)
    except ConfigError as exc:
        out(f"Error: {exc}")
        return 1
    plan(found, data.get("mcp_servers") or {}, project=args.project)
    out(f"MCP servers found in other agents (target: {path}):")
    for item in found:
        for line in describe(item):
            out("  " + line)
    ready = [f for f in found if not f.skip]
    if args.list or not ready:
        if not ready:
            out("Nothing to import.")
        return 0

    if not args.yes:
        if interactive is None:
            interactive = sys.stdin.isatty()
        if not interactive:
            out("Not running interactively: re-run with --yes after reviewing the above.")
            return 1
        prompter = prompter or cw.console_prompter()
        out("Each server's command runs on your machine when coded starts.")
        try:
            if len(ready) == 1:
                chosen = ready if prompter.yes(f"Import {ready[0].name}?") else []
            else:
                names = prompter.choose_many(
                    "Import which? (numbers like 1,3-4, names, or * for all; empty = none)",
                    [f.name for f in ready])
                chosen = [f for f in ready if f.name in names]
        except cw.Abort:
            chosen = []
    else:
        chosen = ready
    if not chosen:
        out("Nothing imported.")
        return 0

    secret = any(f.literal_env for f in chosen)
    try:
        cw.write_file(path, apply(data, chosen), secret=secret)
    except OSError as exc:
        out(f"Error: could not write {path}: {exc}")
        return 1
    if secret and os.name != "nt":
        # write_file keeps an existing file's mode; a copied secret must not
        # stay readable by other users.
        try:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                os.chmod(path, mode & 0o700)
                out(f"{path} now holds secrets: made it readable by you only.")
        except OSError:
            pass
    out(f"Imported {len(chosen)} server(s) into {path}: {', '.join(f.name for f in chosen)}.")
    try:
        import mcp  # noqa: F401
    except ImportError:
        out("MCP support is not installed yet: pip install 'coded[mcp]' (then restart coded).")
    else:
        out("Restart coded to load them; their tools show up as mcp__<server>__<tool>.")
    return 0
