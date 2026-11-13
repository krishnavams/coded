"""Installable skills, plugins and plugin marketplaces.

Layout (``home`` is ``~/.config/coded`` for the user scope, ``./.coded`` for the
project scope)::

    home/installed.json        registry: where each item came from, enabled, …
    home/skills/<name>/        installed skills (also hand-copied ones)
    home/plugins/<name>/       installed plugins
    home/marketplaces/<name>/  cached marketplace checkouts

A plugin is a directory with a manifest — ``.coded-plugin/plugin.json`` (native)
or ``.claude-plugin/plugin.json`` (Claude Code format) — and any of:

    skills/<name>/SKILL.md     skills
    commands/**/*.md           slash commands, namespaced as /<plugin>:<cmd>
    hooks/hooks.json           {"hooks": {"PreToolUse": [...], ...}}
    .mcp.json                  {"mcpServers": {...}} (or a bare server map)
    manifest "external_agents" outside agent CLIs (coded extension)

``${CODED_PLUGIN_ROOT}`` and ``${CLAUDE_PLUGIN_ROOT}`` in hook and MCP commands
expand to the installed plugin directory. Claude Code tool names in hook
matchers (``Bash``, ``Edit|Write``) are mapped to coded's tool names.

Installing is always user-initiated from the CLI; the model has no install tool.
"""

from __future__ import annotations

from functools import reduce

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from coded.config import user_config_path
from coded.skills import parse_frontmatter
from coded.shells import rmtree, shell_path
from coded.sources import Fetched, SourceError, SourceInfo, install_tree, parse_source

MANIFEST_DIRS = (".coded-plugin", ".claude-plugin")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ROOT_VARS = ("${CODED_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}", "$CODED_PLUGIN_ROOT", "$CLAUDE_PLUGIN_ROOT")
_SUPPORTED_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")

# Claude Code tool name -> coded tool name(s).
TOOL_NAME_MAP = {
    "Bash": "bash", "Read": "read", "Write": "write", "Edit": "edit|multi_edit|apply_patch",
    "MultiEdit": "multi_edit", "Glob": "glob", "Grep": "grep", "LS": "ls",
    "WebFetch": "web_fetch", "WebSearch": "web_search", "Task": "task",
    "TodoWrite": "todo_write", "NotebookEdit": "notebook_edit", "ExitPlanMode": "exit_plan_mode",
}

ConfirmFn = Callable[[str], bool]


class InstallError(Exception):
    pass


# ---------------------------------------------------------------------------
# Scopes and the registry
# ---------------------------------------------------------------------------
def scope_home(scope: str = "user", cwd: Optional[str] = None) -> Path:
    if scope == "project":
        return Path(cwd or os.getcwd()) / ".coded"
    if scope != "user":
        raise InstallError(f"Unknown scope {scope!r} (use 'user' or 'project').")
    return user_config_path().parent


class Registry:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "installed.json"
        try:
            self.data: Dict[str, Dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}
        for key in ("skills", "plugins", "marketplaces"):
            self.data.setdefault(key, {})

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def section(self, kind: str) -> Dict[str, Any]:
        return self.data[kind]


def _check_name(name: str, what: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise InstallError(f"Invalid {what} name {name!r}: use lowercase letters, digits, '.', '_' "
                           "or '-' (max 64 chars).")
    return name


# ---------------------------------------------------------------------------
# File inspection (shown to the user before installing)
# ---------------------------------------------------------------------------
def notable_files(root: Path) -> List[str]:
    """Files that can execute or aren't plain docs — worth a look before trusting."""
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        executable = os.access(p, os.X_OK)
        if executable or p.suffix.lower() not in (".md", ".txt", ".json", ".png", ".jpg", ".svg", ""):
            out.append(rel + ("  (executable)" if executable else ""))
    return out


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------
@dataclass
class SkillCandidate:
    name: str
    description: str
    directory: Path
    allowed_tools: List[str] = field(default_factory=list)


def find_skills(root: Path) -> List[SkillCandidate]:
    """A source can be one skill (SKILL.md at its root) or hold several."""
    dirs: List[Path] = []
    if (root / "SKILL.md").is_file():
        dirs = [root]
    else:
        for pattern in ("skills/*/SKILL.md", "*/SKILL.md"):
            dirs = [p.parent for p in sorted(root.glob(pattern))]
            if dirs:
                break
    if not dirs:
        # Collections nest deeper: plugins/<p>/skills/<name>/, .claude/skills/<name>/.
        dirs = _deep_skill_dirs(root)
    out = []
    for d in dirs:
        meta, _ = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
        name = str(meta.get("name") or d.name).strip()
        desc = str(meta.get("description") or "").strip()
        tools = meta.get("allowed-tools") or meta.get("allowed_tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        out.append(SkillCandidate(name, desc, d, list(tools)))
    return out


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
_MAX_DEPTH = 5


def _deep_skill_dirs(root: Path) -> List[Path]:
    """Directories holding a SKILL.md up to `_MAX_DEPTH` below `root`. A skill's
    own sub-folders are not searched (its examples are not more skills)."""
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        if "SKILL.md" in filenames and here != root:
            found.append(here)
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS) if depth < _MAX_DEPTH else []
    return found


def _skill_summary(cands: List[SkillCandidate], info: SourceInfo, dest: Path) -> str:
    lines = [f"Install {len(cands)} skill(s) from {info.describe()} into {dest}:"]
    for c in cands:
        lines.append(f"  • {c.name}: {c.description or '(no description)'}")
        if c.allowed_tools:
            lines.append(f"      allowed-tools: {', '.join(c.allowed_tools)}")
        files = notable_files(c.directory)
        if files:
            lines.append("      files the agent may run or read:")
            lines += [f"        - {f}" for f in files[:30]]
            if len(files) > 30:
                lines.append(f"        … and {len(files) - 30} more")
    lines.append("Skills are instructions the agent follows, and may include scripts it runs. "
                 "Only install skills you trust.")
    return "\n".join(lines)


def install_skills(source: str, *, names: Optional[List[str]] = None, install_all: bool = False,
                   scope: str = "user", cwd: Optional[str] = None, ref: Optional[str] = None,
                   subpath: Optional[str] = None, confirm: Optional[ConfirmFn] = None,
                   force: bool = False, _info: Optional[SourceInfo] = None) -> List[str]:
    """Install skill(s) from a source. Returns the installed names."""
    from coded.sources import skills_sh_page

    page = None if _info else skills_sh_page(source)
    if page:  # a skills.sh link names the repository and, maybe, one skill in it
        source = page[0]
        if page[1] and not names and not install_all:
            names = [page[1]]
    info = _info or parse_source(source, ref, subpath)
    home = scope_home(scope, cwd)
    with Fetched(info) as fetched:
        cands = find_skills(fetched.root)
        if not cands:
            raise InstallError(f"No SKILL.md found in this source (searched {_MAX_DEPTH} "
                               "folders deep). Use --path to point at the skill's folder.")
        seen: Dict[str, Path] = {}
        for c in cands:
            if c.name in seen:
                raise InstallError(
                    f"Two skills in this source are both named '{c.name}' "
                    f"({seen[c.name].relative_to(fetched.root).as_posix()} and "
                    f"{c.directory.relative_to(fetched.root).as_posix()}). Pick one with --path.")
            seen[c.name] = c.directory
            _check_name(c.name, "skill")
            if not c.description:
                raise InstallError(f"Skill '{c.name}' has no description in its frontmatter; "
                                   "the description is what tells the agent when to use it.")
        if names:
            missing = set(names) - {c.name for c in cands}
            if missing:
                raise InstallError(f"Not in this source: {', '.join(sorted(missing))}. "
                                   f"Available: {', '.join(c.name for c in cands)}")
            cands = [c for c in cands if c.name in names]
        elif len(cands) > 1 and not install_all:
            raise InstallError("This source has several skills: "
                               + ", ".join(c.name for c in cands)
                               + ". Pass their names, or --all.")
        reg = Registry(home)
        for c in cands:
            fetched.refuse_links_in(c.directory)
            if (home / "skills" / c.name).exists() and not force and c.name not in reg.section("skills"):
                raise InstallError(f"A skill named '{c.name}' already exists in {home / 'skills'} "
                                   "(installed by hand). Use --force to replace it.")
        if confirm is not None and not confirm(_skill_summary(cands, info, home / "skills")):
            raise InstallError("Cancelled.")
        for c in cands:
            install_tree(c.directory, home / "skills" / c.name)
            rel = c.directory.relative_to(fetched.root).as_posix()
            src = info.to_dict()
            if rel != ".":
                src["subpath"] = "/".join(x for x in (info.subpath, rel) if x)
            reg.section("skills")[c.name] = {"source": src, "installed_at": int(time.time())}
        reg.save()
        return [c.name for c in cands]


def remove_skill(name: str, *, scope: str = "user", cwd: Optional[str] = None) -> None:
    home = scope_home(scope, cwd)
    path = home / "skills" / name
    reg = Registry(home)
    if not path.exists() and name not in reg.section("skills"):
        raise InstallError(f"No skill '{name}' in {home / 'skills'}.")
    rmtree(path)
    reg.section("skills").pop(name, None)
    reg.save()


def update_skills(names: Optional[List[str]] = None, *, scope: str = "user", cwd: Optional[str] = None,
                  confirm: Optional[ConfirmFn] = None) -> List[Tuple[str, str]]:
    """Re-fetch skills from their recorded sources. Returns (name, result) pairs."""
    reg = Registry(scope_home(scope, cwd))
    targets = names or list(reg.section("skills"))
    results = []
    for name in targets:
        rec = reg.section("skills").get(name)
        if rec is None:
            results.append((name, "not installed with `coded skill install` (no recorded source)"))
            continue
        info = SourceInfo.from_dict(rec["source"])
        info.commit = None
        try:
            install_skills(info.location, names=[name], scope=scope, cwd=cwd, confirm=confirm,
                           force=True, _info=info)
            results.append((name, "updated"))
        except (InstallError, SourceError) as exc:
            results.append((name, f"failed, old version kept: {exc}"))
    return results


def list_skills(cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    """Installed (non built-in) skills in both scopes, with their source if known."""
    out = []
    for scope in ("user", "project"):
        home = scope_home(scope, cwd)
        reg = Registry(home)
        base = home / "skills"
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not (d / "SKILL.md").is_file():
                continue
            meta, _ = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
            rec = reg.section("skills").get(d.name)
            out.append({"name": str(meta.get("name") or d.name), "scope": scope,
                        "description": str(meta.get("description") or ""),
                        "source": SourceInfo.from_dict(rec["source"]).describe() if rec else "(copied by hand)",
                        "path": str(d)})
    return out


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------
@dataclass
class Plugin:
    name: str
    root: Path
    manifest: Dict[str, Any]
    format: str  # "coded" | "claude"
    scope: str = "user"
    enabled: bool = True
    source: Optional[Dict[str, Any]] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def version(self) -> str:
        return str(self.manifest.get("version") or "")

    @property
    def description(self) -> str:
        return str(self.manifest.get("description") or "")

    # -- components -----------------------------------------------------------
    def skill_dirs(self) -> List[Path]:
        dirs = [self.root / "skills"]
        for extra in _as_paths(self.manifest.get("skills")):
            dirs.append(self.root / extra)
        return [d for d in dirs if d.is_dir()]

    def skills(self) -> List[SkillCandidate]:
        out: List[SkillCandidate] = []
        for d in self.skill_dirs():
            out += find_skills(d)
        return out

    def command_dirs(self) -> List[Path]:
        dirs = [self.root / "commands"]
        for extra in _as_paths(self.manifest.get("commands")):
            dirs.append(self.root / extra)
        return [d for d in dirs if d.is_dir()]

    def commands(self) -> List[str]:
        names = []
        for d in self.command_dirs():
            for md in sorted(d.rglob("*.md")):
                names.append(f"{self.name}:" + ":".join(md.relative_to(d).with_suffix("").parts))
        return names

    def _expand(self, text: str, for_shell: bool = False) -> str:
        """Substitute ${CODED_PLUGIN_ROOT}/${CLAUDE_PLUGIN_ROOT}. In a command
        string run by a shell, the path is written the way that shell reads it
        (forward slashes under Git Bash, which escapes with backslashes)."""
        root = shell_path(self.root) if for_shell else str(self.root)
        for var in _ROOT_VARS:
            text = text.replace(var, root)
        return text

    def _expand_tree(self, value: Any) -> Any:
        """Expand the plugin root inside a parsed structure. Substituting into
        JSON text instead would produce invalid JSON for Windows paths, whose
        backslashes are escape characters."""
        if isinstance(value, str):
            return self._expand(value)
        if isinstance(value, dict):
            return {k: self._expand_tree(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._expand_tree(v) for v in value]
        return value

    def hooks(self) -> Dict[str, List[Dict[str, Any]]]:
        """Hook entries in coded's flat form, paths expanded and matchers mapped."""
        raw = self.manifest.get("hooks")
        if isinstance(raw, str):
            raw = _read_json(self.root / raw)
        if raw is None and (self.root / "hooks" / "hooks.json").is_file():
            raw = _read_json(self.root / "hooks" / "hooks.json")
        if isinstance(raw, dict) and "hooks" in raw and isinstance(raw["hooks"], dict):
            raw = raw["hooks"]
        out: Dict[str, List[Dict[str, Any]]] = {}
        for event, entries in (raw or {}).items():
            if event not in _SUPPORTED_EVENTS:
                self._warn(f"hook event '{event}' is not supported by coded; ignored")
                continue
            for entry in entries or []:
                for hook in _flatten_hook(entry):
                    if hook.get("type", "command") != "command" or not hook.get("command"):
                        self._warn(f"{event} hook of type '{hook.get('type')}' is not supported; ignored")
                        continue
                    matcher = hook.get("matcher")
                    if event in ("PreToolUse", "PostToolUse") and matcher:
                        matcher = self._map_matcher(matcher)
                    elif event not in ("PreToolUse", "PostToolUse"):
                        matcher = None  # coded doesn't match session/prompt sources
                    out.setdefault(event, []).append({
                        "matcher": matcher,
                        "command": self._expand(hook["command"], for_shell=True),
                        "timeout": hook.get("timeout", 60),
                        "env": {"CODED_PLUGIN_ROOT": str(self.root),
                                "CLAUDE_PLUGIN_ROOT": str(self.root)},
                        "plugin": self.name,
                    })
        return out

    def _map_matcher(self, matcher: str) -> str:
        if matcher == "*":
            return matcher
        parts = matcher.split("|")
        if all(re.fullmatch(r"[A-Za-z_][\w]*", p) for p in parts):
            mapped = []
            for p in parts:
                if p in TOOL_NAME_MAP:
                    mapped.append(TOOL_NAME_MAP[p])
                elif p.startswith("mcp__") or p == p.lower():
                    mapped.append(p)
                else:
                    self._warn(f"hook matcher tool '{p}' has no coded equivalent")
                    mapped.append(p)
            return "|".join(mapped)
        self._warn(f"complex hook matcher {matcher!r} kept as-is; check it targets coded tool names")
        return matcher

    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        raw = self.manifest.get("mcpServers") or self.manifest.get("mcp_servers")
        if isinstance(raw, str):
            raw = _read_json(self.root / raw)
        if raw is None and (self.root / ".mcp.json").is_file():
            raw = _read_json(self.root / ".mcp.json")
        if isinstance(raw, dict) and isinstance(raw.get("mcpServers"), dict):
            raw = raw["mcpServers"]
        out = {}
        for name, spec in (raw or {}).items():
            if not isinstance(spec, dict):
                continue
            if not spec.get("command"):
                self._warn(f"MCP server '{name}' is not a stdio (command) server; coded supports "
                           "stdio only, ignored")
                continue
            spec = self._expand_tree(spec)
            spec.setdefault("env", {})
            spec["env"] = {**spec["env"], "CODED_PLUGIN_ROOT": str(self.root),
                           "CLAUDE_PLUGIN_ROOT": str(self.root)}
            out[f"{self.name}-{name}"] = spec
        return out

    def external_agents(self) -> Dict[str, Any]:
        raw = self.manifest.get("external_agents") or {}
        return json.loads(self._expand(json.dumps(raw))) if isinstance(raw, dict) else {}

    def unsupported(self) -> None:
        if (self.root / "agents").is_dir() and any((self.root / "agents").glob("*.md")):
            self._warn("agents/ (custom sub-agent definitions) are not supported by coded; ignored")
        for key in ("lspServers", "outputStyles"):
            if self.manifest.get(key):
                self._warn(f"'{key}' is not supported by coded; ignored")

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def summary(self, info: Optional[SourceInfo] = None) -> str:
        """Everything the plugin will add — shown verbatim before install/update."""
        self.warnings.clear()
        self.unsupported()
        hooks, mcp, agents = self.hooks(), self.mcp_servers(), self.external_agents()
        lines = [f"Plugin '{self.name}' {self.version}".rstrip() + f" ({self.format} format)"]
        if info is not None:
            lines.append(f"  source: {info.describe()}")
        if self.description:
            lines.append(f"  {self.description}")
        skills = self.skills()
        if skills:
            lines.append(f"  skills ({len(skills)}): " + ", ".join(s.name for s in skills))
        cmds = self.commands()
        if cmds:
            lines.append(f"  commands ({len(cmds)}): " + ", ".join("/" + c for c in cmds))
        if hooks:
            lines.append("  HOOKS — these commands run automatically, without asking:")
            for event, entries in hooks.items():
                for h in entries:
                    m = f" [{h['matcher']}]" if h.get("matcher") else ""
                    lines.append(f"    {event}{m}: {h['command']}")
        if mcp:
            lines.append("  MCP SERVERS — started when coded starts:")
            for name, spec in mcp.items():
                lines.append(f"    {name}: {' '.join([spec['command'], *map(str, spec.get('args', []))])}")
        if agents:
            lines.append("  outside agents: " + ", ".join(agents))
        files = notable_files(self.root)
        if files:
            lines.append(f"  files that can run ({len(files)}): " + ", ".join(files[:15])
                         + (" …" if len(files) > 15 else ""))
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        roots = {str(self.root), shell_path(self.root)}
        lines = [reduce(lambda ln, r: ln.replace(r, "${PLUGIN_ROOT}"), roots, ln) for ln in lines]
        if hooks or mcp:
            lines.append("Installing this plugin lets it run the commands above on your machine.")
        return "\n".join(lines)


def _as_paths(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if isinstance(v, str)]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"Could not read {path}: {exc}") from exc


def _flatten_hook(entry: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(entry, str):
        yield {"command": entry}
    elif isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
        for h in entry["hooks"]:
            if isinstance(h, dict):
                yield {**h, "matcher": entry.get("matcher")}
    elif isinstance(entry, dict):
        yield entry


def load_plugin(root: Path, scope: str = "user") -> Plugin:
    for d in MANIFEST_DIRS:
        mf = root / d / "plugin.json"
        if mf.is_file():
            manifest = _read_json(mf)
            fmt = "coded" if d == ".coded-plugin" else "claude"
            break
    else:
        if (root / "plugin.json").is_file():
            manifest, fmt = _read_json(root / "plugin.json"), "coded"
        else:
            raise InstallError("No plugin manifest found (.coded-plugin/plugin.json, "
                               ".claude-plugin/plugin.json or plugin.json).")
    if not isinstance(manifest, dict):
        raise InstallError("Plugin manifest must be a JSON object.")
    name = _check_name(str(manifest.get("name") or ""), "plugin")
    return Plugin(name=name, root=root, manifest=manifest, format=fmt, scope=scope)


def _ask(confirm: Optional[ConfirmFn], text: str) -> None:
    if confirm is not None and not confirm(text):
        raise InstallError("Cancelled.")


def install_plugin(source: str, *, scope: str = "user", cwd: Optional[str] = None,
                   ref: Optional[str] = None, subpath: Optional[str] = None,
                   confirm: Optional[ConfirmFn] = None, _info: Optional[SourceInfo] = None,
                   _marketplace: Optional[str] = None, _replace: bool = False) -> Plugin:
    if "@" in source and not _info and _marketplace is None and not Path(source).exists() \
            and "://" not in source and not source.startswith("git@") and "/" not in source.split("@")[0]:
        name, market = source.split("@", 1)
        return install_from_marketplace(name, market, scope=scope, cwd=cwd, confirm=confirm)
    info = _info or parse_source(source, ref, subpath)
    home = scope_home(scope, cwd)
    reg = Registry(home)
    with Fetched(info) as fetched:
        fetched.refuse_links_in(fetched.root)  # a plugin is installed whole
        plugin = load_plugin(fetched.root, scope)
        if plugin.name in reg.section("plugins") and not _replace:
            raise InstallError(f"Plugin '{plugin.name}' is already installed; use "
                               "`coded plugin update` or remove it first.")
        if not (plugin.skills() or plugin.commands() or plugin.hooks() or plugin.mcp_servers()
                or plugin.external_agents()):
            raise InstallError(f"Plugin '{plugin.name}' contributes nothing coded can use.")
        _ask(confirm, plugin.summary(info))
        dest = home / "plugins" / plugin.name
        install_tree(fetched.root, dest)
    old = reg.section("plugins").get(plugin.name, {})
    reg.section("plugins")[plugin.name] = {
        "source": info.to_dict(), "version": plugin.version, "format": plugin.format,
        "enabled": old.get("enabled", True), "installed_at": int(time.time()),
        **({"marketplace": _marketplace} if _marketplace else {}),
    }
    reg.save()
    return load_plugin(home / "plugins" / plugin.name, scope)


def update_plugins(names: Optional[List[str]] = None, *, scope: str = "user",
                   cwd: Optional[str] = None, confirm: Optional[ConfirmFn] = None) -> List[Tuple[str, str]]:
    reg = Registry(scope_home(scope, cwd))
    results = []
    for name in names or list(reg.section("plugins")):
        rec = reg.section("plugins").get(name)
        if rec is None:
            results.append((name, "not installed"))
            continue
        try:
            if rec.get("marketplace"):
                update_marketplaces([rec["marketplace"]], scope="user", cwd=cwd)
                p = install_from_marketplace(name, rec["marketplace"], scope=scope, cwd=cwd,
                                             confirm=confirm, _replace=True)
            else:
                info = SourceInfo.from_dict(rec["source"])
                info.commit = None
                p = install_plugin(info.location, scope=scope, cwd=cwd, confirm=confirm,
                                   _info=info, _replace=True)
            if p.name != name:
                results.append((name, f"source now provides '{p.name}'"))
            else:
                results.append((name, f"updated to {p.version or 'latest'}"))
        except (InstallError, SourceError) as exc:
            results.append((name, f"failed, old version kept: {exc}"))
    return results


def remove_plugin(name: str, *, scope: str = "user", cwd: Optional[str] = None) -> None:
    home = scope_home(scope, cwd)
    reg = Registry(home)
    if name not in reg.section("plugins") and not (home / "plugins" / name).exists():
        raise InstallError(f"No plugin '{name}' installed in {scope} scope.")
    rmtree(home / "plugins" / name)
    reg.section("plugins").pop(name, None)
    reg.save()


def set_plugin_enabled(name: str, enabled: bool, *, scope: str = "user", cwd: Optional[str] = None) -> None:
    reg = Registry(scope_home(scope, cwd))
    rec = reg.section("plugins").get(name)
    if rec is None:
        raise InstallError(f"No plugin '{name}' installed in {scope} scope.")
    rec["enabled"] = enabled
    reg.save()


def installed_plugins(cwd: Optional[str] = None, *, include_disabled: bool = False) -> List[Plugin]:
    """Plugins from both scopes (a project plugin overrides a user one of the same name)."""
    found: Dict[str, Plugin] = {}
    for scope in ("user", "project"):
        home = scope_home(scope, cwd)
        reg = Registry(home)
        for name, rec in reg.section("plugins").items():
            root = home / "plugins" / name
            if not root.is_dir():
                continue
            try:
                p = load_plugin(root, scope)
            except InstallError:
                continue
            p.enabled = bool(rec.get("enabled", True))
            p.source = rec.get("source")
            if p.enabled or include_disabled:
                found[p.name] = p
            else:
                found.pop(p.name, None)
    return list(found.values())


# ---------------------------------------------------------------------------
# What enabled plugins contribute (used by skills/commands/config loading)
# ---------------------------------------------------------------------------
def plugin_skill_dirs(cwd: Optional[str] = None) -> List[Path]:
    dirs: List[Path] = []
    for p in installed_plugins(cwd):
        for d in p.skill_dirs():
            dirs.append(d)
    return dirs


def plugin_command_dirs(cwd: Optional[str] = None) -> List[Tuple[Path, str]]:
    return [(d, p.name) for p in installed_plugins(cwd) for d in p.command_dirs()]


def apply_plugins_to_config(cfg: Any, cwd: Optional[str] = None,
                            include_project: bool = True) -> List[str]:
    """Merge enabled plugins' hooks (appended), MCP servers and outside agents into
    a loaded Config. User config wins on name clashes. Returns warnings.
    Project-scope plugins are skipped for an untrusted project."""
    warnings: List[str] = []
    try:
        plugins = installed_plugins(cwd)
    except Exception as exc:  # noqa: BLE001 - a broken registry must not stop coded
        return [f"Could not load plugins: {exc}"]
    for p in plugins:
        if not include_project and getattr(p, "scope", "user") == "project":
            continue
        try:
            hooks = p.hooks()
            mcp = p.mcp_servers()
            agents = p.external_agents()
        except InstallError as exc:
            warnings.append(f"plugin '{p.name}': {exc}")
            continue
        merged = dict(cfg.hooks)
        for event, entries in hooks.items():
            merged[event] = list(merged.get(event) or []) + entries
        cfg.hooks = merged
        for name, spec in mcp.items():
            cfg.mcp_servers.setdefault(name, spec)
        for name, spec in agents.items():
            cfg.external_agents.setdefault(name, spec)
        cfg.plugins.append(p.name)
    return warnings


# ---------------------------------------------------------------------------
# Marketplaces
# ---------------------------------------------------------------------------
_MARKET_FILES = (".coded-plugin/marketplace.json", ".claude-plugin/marketplace.json", "marketplace.json")


def _load_market_index(root: Path) -> Dict[str, Any]:
    for rel in _MARKET_FILES:
        if (root / rel).is_file():
            data = _read_json(root / rel)
            if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
                raise InstallError(f"{rel} must be an object with a 'plugins' list.")
            return data
    raise InstallError("No marketplace index found (.claude-plugin/marketplace.json, "
                       ".coded-plugin/marketplace.json or marketplace.json).")


def add_marketplace(source: str, *, name: Optional[str] = None, scope: str = "user",
                    cwd: Optional[str] = None, ref: Optional[str] = None,
                    _replace: bool = False) -> Tuple[str, int]:
    info = parse_source(source, ref)
    home = scope_home(scope, cwd)
    reg = Registry(home)
    with Fetched(info) as fetched:
        fetched.refuse_links_in(fetched.root)
        index = _load_market_index(fetched.root)
        mname = _check_name(name or str(index.get("name") or ""), "marketplace")
        if mname in reg.section("marketplaces") and not _replace:
            raise InstallError(f"Marketplace '{mname}' already added; use "
                               "`coded plugin marketplace update`.")
        install_tree(fetched.root, home / "marketplaces" / mname)
    reg.section("marketplaces")[mname] = {"source": info.to_dict(), "updated_at": int(time.time())}
    reg.save()
    return mname, len(index["plugins"])


def update_marketplaces(names: Optional[List[str]] = None, *, scope: str = "user",
                        cwd: Optional[str] = None) -> List[Tuple[str, str]]:
    reg = Registry(scope_home(scope, cwd))
    out = []
    for name in names or list(reg.section("marketplaces")):
        rec = reg.section("marketplaces").get(name)
        if rec is None:
            out.append((name, "not added"))
            continue
        info = SourceInfo.from_dict(rec["source"])
        try:
            _, n = add_marketplace(info.location, name=name, scope=scope, cwd=cwd, ref=info.ref,
                                   _replace=True)
            out.append((name, f"{n} plugin(s)"))
        except (InstallError, SourceError) as exc:
            out.append((name, f"failed, cached copy kept: {exc}"))
    return out


def remove_marketplace(name: str, *, scope: str = "user", cwd: Optional[str] = None) -> None:
    home = scope_home(scope, cwd)
    reg = Registry(home)
    if name not in reg.section("marketplaces"):
        raise InstallError(f"No marketplace '{name}'.")
    rmtree(home / "marketplaces" / name)
    reg.section("marketplaces").pop(name)
    reg.save()


def marketplace_entries(cwd: Optional[str] = None, scope: str = "user") -> List[Dict[str, Any]]:
    home = scope_home(scope, cwd)
    out = []
    for mname in Registry(home).section("marketplaces"):
        root = home / "marketplaces" / mname
        try:
            index = _load_market_index(root)
        except InstallError:
            continue
        for entry in index["plugins"]:
            if isinstance(entry, dict) and entry.get("name"):
                out.append({**entry, "marketplace": mname})
    return out


def _entry_source(entry: Dict[str, Any], market_root: Path) -> SourceInfo:
    src = entry.get("source")
    if isinstance(src, str):
        if src.startswith(("./", "../")) or src in (".", "./"):
            path = (market_root / src).resolve()
            if market_root.resolve() not in path.parents and path != market_root.resolve():
                raise InstallError(f"Plugin source escapes the marketplace: {src}")
            return SourceInfo("dir", str(path))
        return parse_source(src)
    if isinstance(src, dict):
        kind = src.get("source")
        if kind == "github" and src.get("repo"):
            return SourceInfo("github", f"https://github.com/{src['repo']}.git", src.get("ref"),
                              src.get("path"))
        if kind in ("url", "git") and src.get("url"):
            return parse_source(src["url"], src.get("ref"), src.get("path"))
    raise InstallError(f"Unsupported source for plugin '{entry.get('name')}': {src!r}")


def install_from_marketplace(name: str, market: str, *, scope: str = "user", cwd: Optional[str] = None,
                             confirm: Optional[ConfirmFn] = None, _replace: bool = False) -> Plugin:
    home = scope_home("user", cwd)  # marketplaces are user-level
    if market not in Registry(home).section("marketplaces"):
        raise InstallError(f"Unknown marketplace '{market}'. Add it with "
                           "`coded plugin marketplace add <source>`.")
    root = home / "marketplaces" / market
    entry = next((e for e in _load_market_index(root)["plugins"] if e.get("name") == name), None)
    if entry is None:
        raise InstallError(f"Marketplace '{market}' has no plugin '{name}'.")
    info = _entry_source(entry, root)
    return install_plugin(info.location, scope=scope, cwd=cwd, confirm=confirm, _info=info,
                          _marketplace=market, _replace=_replace)
