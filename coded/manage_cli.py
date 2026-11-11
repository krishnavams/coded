"""`coded skill …` and `coded plugin …` subcommands."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from rich.table import Table

from coded import ui
from coded.plugins import (
    InstallError,
    add_marketplace,
    install_plugin,
    install_skills,
    installed_plugins,
    list_skills,
    marketplace_entries,
    remove_marketplace,
    remove_plugin,
    remove_skill,
    set_plugin_enabled,
    update_marketplaces,
    update_plugins,
    update_skills,
    Registry,
    scope_home,
)
from coded.sources import SourceError

RESTART = "Restart coded (or start a new session) to load the changes."


def _confirm_fn(assume_yes: bool):
    """None = no confirmation needed; otherwise ask on a TTY, refuse without one."""
    if assume_yes:
        return None

    def confirm(summary: str) -> bool:
        ui.console.print(summary)
        if not sys.stdin.isatty():
            ui.error("Not running interactively: re-run with --yes after reviewing the above.")
            return False
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")

    return confirm


def _common(p: argparse.ArgumentParser, scope: bool = True) -> None:
    if scope:
        p.add_argument("--project", action="store_true",
                       help="Use the project scope (./.coded, gitignored by default) instead of the user scope.")
    p.add_argument("--cwd", help="Project directory (default: current dir).")


def _scope(args) -> str:
    return "project" if getattr(args, "project", False) else "user"


def _cwd(args) -> str:
    return os.path.abspath(args.cwd) if getattr(args, "cwd", None) else os.getcwd()


def _source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", help="Local dir, .zip (path or URL), git URL, or GitHub owner/repo[/path][@ref].")
    p.add_argument("--ref", help="Git branch, tag or commit.")
    p.add_argument("--path", dest="subpath", help="Sub-directory inside the source.")
    p.add_argument("-y", "--yes", action="store_true", help="Don't ask for confirmation.")


# ---------------------------------------------------------------------------
# coded skill
# ---------------------------------------------------------------------------
def skill_main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="coded skill", description="Install and manage skills.")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("install", help="Install skill(s) from a source.")
    _source_args(pi)
    pi.add_argument("names", nargs="*", help="Which skills to install, if the source has several.")
    pi.add_argument("--all", action="store_true", help="Install every skill in the source.")
    pi.add_argument("--force", action="store_true", help="Replace a hand-copied skill of the same name.")
    _common(pi)
    pl = sub.add_parser("list", help="List installed skills (built-ins excluded).")
    _common(pl, scope=False)
    pr = sub.add_parser("remove", help="Remove an installed skill.")
    pr.add_argument("name")
    _common(pr)
    pu = sub.add_parser("update", help="Re-fetch skills from their recorded sources.")
    pu.add_argument("names", nargs="*")
    pu.add_argument("-y", "--yes", action="store_true")
    _common(pu)
    pn = sub.add_parser("info", help="Show a skill's details.")
    pn.add_argument("name")
    _common(pn, scope=False)
    args = p.parse_args(argv)

    try:
        if args.cmd == "install":
            names = install_skills(args.source, names=args.names or None, install_all=args.all,
                                   scope=_scope(args), cwd=_cwd(args), ref=args.ref,
                                   subpath=args.subpath, confirm=_confirm_fn(args.yes),
                                   force=args.force)
            ui.success(f"Installed skill(s): {', '.join(names)} ({_scope(args)} scope). {RESTART}")
        elif args.cmd == "list":
            rows = list_skills(_cwd(args))
            if not rows:
                ui.info("No installed skills. Built-in roles: planner, reviewer, qa, security.")
                return 0
            table = Table(title="Installed skills")
            for col in ("skill", "scope", "source", "description"):
                table.add_column(col)
            for r in rows:
                table.add_row(r["name"], r["scope"], r["source"], r["description"][:70])
            ui.console.print(table)
        elif args.cmd == "remove":
            remove_skill(args.name, scope=_scope(args), cwd=_cwd(args))
            ui.success(f"Removed skill '{args.name}'.")
        elif args.cmd == "update":
            results = update_skills(args.names or None, scope=_scope(args), cwd=_cwd(args),
                                    confirm=_confirm_fn(args.yes))
            if not results:
                ui.info("No skills installed from a source.")
            for name, result in results:
                ui.info(f"{name}: {result}")
            return 1 if any(r.startswith("failed") for _, r in results) else 0
        elif args.cmd == "info":
            from coded.skills import discover_skills

            skill = discover_skills(_cwd(args)).get(args.name)
            if skill is None:
                ui.error(f"No skill '{args.name}'.")
                return 1
            ui.console.print(f"[bold]{skill.name}[/bold]  {skill.path}")
            ui.console.print(skill.description)
            if skill.allowed_tools:
                ui.console.print(f"allowed-tools: {', '.join(skill.allowed_tools)}")
    except (InstallError, SourceError) as exc:
        ui.error(str(exc))
        return 1
    return 0


# ---------------------------------------------------------------------------
# coded plugin
# ---------------------------------------------------------------------------
def plugin_main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="coded plugin",
                                description="Install and manage plugins (skills + commands + hooks "
                                            "+ MCP servers + outside agents) and marketplaces.")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("install", help="Install a plugin from a source, or <name>@<marketplace>.")
    _source_args(pi)
    _common(pi)
    pl = sub.add_parser("list", help="List installed plugins.")
    _common(pl, scope=False)
    for name, helptext in (("remove", "Uninstall a plugin."), ("enable", "Enable a plugin."),
                           ("disable", "Disable a plugin without removing it.")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("name")
        _common(sp)
    pu = sub.add_parser("update", help="Re-fetch plugins from their sources.")
    pu.add_argument("names", nargs="*")
    pu.add_argument("-y", "--yes", action="store_true")
    _common(pu)
    pn = sub.add_parser("info", help="Show everything a plugin contributes.")
    pn.add_argument("name")
    _common(pn, scope=False)
    ps = sub.add_parser("search", help="List plugins offered by added marketplaces.")
    ps.add_argument("query", nargs="?", default="")
    _common(ps, scope=False)
    pm = sub.add_parser("marketplace", help="Manage plugin marketplaces.")
    msub = pm.add_subparsers(dest="mcmd", required=True)
    ma = msub.add_parser("add", help="Add a marketplace (dir, zip, git URL, owner/repo).")
    ma.add_argument("source")
    ma.add_argument("--name", help="Override the marketplace name.")
    ma.add_argument("--ref")
    msub.add_parser("list", help="List added marketplaces.")
    mr = msub.add_parser("remove")
    mr.add_argument("name")
    mu = msub.add_parser("update")
    mu.add_argument("names", nargs="*")
    args = p.parse_args(argv)
    cwd = _cwd(args)

    try:
        if args.cmd == "install":
            plugin = install_plugin(args.source, scope=_scope(args), cwd=cwd, ref=args.ref,
                                    subpath=args.subpath, confirm=_confirm_fn(args.yes))
            ui.success(f"Installed plugin '{plugin.name}' {plugin.version} ({_scope(args)} scope). {RESTART}")
            for w in plugin.warnings:
                ui.warn(w)
        elif args.cmd == "list":
            plugins = installed_plugins(cwd, include_disabled=True)
            if not plugins:
                ui.info("No plugins installed. Try `coded plugin install <source>` or "
                        "`coded plugin marketplace add <source>`.")
                return 0
            table = Table(title="Installed plugins")
            for col in ("plugin", "version", "scope", "status", "provides"):
                table.add_column(col)
            for pl_ in plugins:
                provides = []
                if pl_.skills():
                    provides.append(f"{len(pl_.skills())} skills")
                if pl_.commands():
                    provides.append(f"{len(pl_.commands())} commands")
                if pl_.hooks():
                    provides.append("hooks")
                if pl_.mcp_servers():
                    provides.append(f"{len(pl_.mcp_servers())} MCP")
                if pl_.external_agents():
                    provides.append("agents")
                table.add_row(pl_.name, pl_.version, pl_.scope,
                              "[green]enabled[/green]" if pl_.enabled else "[dim]disabled[/dim]",
                              ", ".join(provides))
            ui.console.print(table)
        elif args.cmd == "remove":
            remove_plugin(args.name, scope=_scope(args), cwd=cwd)
            ui.success(f"Removed plugin '{args.name}'. {RESTART}")
        elif args.cmd in ("enable", "disable"):
            set_plugin_enabled(args.name, args.cmd == "enable", scope=_scope(args), cwd=cwd)
            ui.success(f"Plugin '{args.name}' {args.cmd}d. {RESTART}")
        elif args.cmd == "update":
            results = update_plugins(args.names or None, scope=_scope(args), cwd=cwd,
                                     confirm=_confirm_fn(args.yes))
            if not results:
                ui.info("No plugins installed.")
            for name, result in results:
                ui.info(f"{name}: {result}")
            return 1 if any(r.startswith("failed") for _, r in results) else 0
        elif args.cmd == "info":
            match = [x for x in installed_plugins(cwd, include_disabled=True) if x.name == args.name]
            if not match:
                ui.error(f"No plugin '{args.name}'.")
                return 1
            pl_ = match[0]
            ui.console.print(pl_.summary())
            ui.console.print(f"  location: {pl_.root}   status: "
                             f"{'enabled' if pl_.enabled else 'disabled'}")
        elif args.cmd == "search":
            entries = marketplace_entries(cwd)
            q = args.query.lower()
            entries = [e for e in entries if q in e["name"].lower()
                       or q in str(e.get("description", "")).lower()]
            if not entries:
                ui.info("No matching plugins. Add a marketplace with "
                        "`coded plugin marketplace add <source>`.")
                return 0
            installed = {x.name for x in installed_plugins(cwd, include_disabled=True)}
            table = Table(title="Available plugins")
            for col in ("plugin", "marketplace", "version", "description", ""):
                table.add_column(col)
            for e in entries:
                table.add_row(e["name"], e["marketplace"], str(e.get("version", "")),
                              str(e.get("description", ""))[:60],
                              "installed" if e["name"] in installed else "")
            ui.console.print(table)
            ui.info("Install with: coded plugin install <plugin>@<marketplace>")
        elif args.cmd == "marketplace":
            return _marketplace(args, cwd)
    except (InstallError, SourceError) as exc:
        ui.error(str(exc))
        return 1
    return 0


def _marketplace(args, cwd: str) -> int:
    if args.mcmd == "add":
        name, n = add_marketplace(args.source, name=args.name, cwd=cwd, ref=args.ref)
        ui.success(f"Added marketplace '{name}' ({n} plugin(s)). "
                   "Browse with `coded plugin search`.")
    elif args.mcmd == "list":
        reg = Registry(scope_home("user", cwd))
        if not reg.section("marketplaces"):
            ui.info("No marketplaces added.")
        for name, rec in reg.section("marketplaces").items():
            from coded.sources import SourceInfo

            print(f"{name:<20} {SourceInfo.from_dict(rec['source']).describe()}")
    elif args.mcmd == "remove":
        remove_marketplace(args.name, cwd=cwd)
        ui.success(f"Removed marketplace '{args.name}' (installed plugins stay installed).")
    elif args.mcmd == "update":
        for name, result in update_marketplaces(args.names or None, cwd=cwd):
            ui.info(f"{name}: {result}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - convenience
    argv = sys.argv[1:] if argv is None else argv
    return skill_main(argv[1:]) if argv[:1] == ["skill"] else plugin_main(argv[1:])
