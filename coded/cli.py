"""Command-line entry point for coded."""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from pathlib import Path
from typing import List, Optional

from coded import __version__, ui
from coded.agent import create_agent
from coded.config import (
    Config,
    ConfigError,
    ModelConfig,
    load_config,
    model_from_overrides,
    write_sample_config,
)
from coded.permissions import PermissionManager
from coded.shells import configure_stdio, resolve_shell, set_active_shell
from coded.telemetry import tracer_from_config


SUBCOMMANDS = {"config", "models"}


def build_parser() -> argparse.ArgumentParser:
    """Parser for the default `run` behaviour (and `models`, which needs no args).

    `config` is dispatched separately (see build_config_parser) because mixing a
    free-form positional prompt with argparse subparsers is unreliable.
    """
    p = argparse.ArgumentParser(
        prog="coded",
        description="A Claude Code-style coding agent for OpenAI-compatible models.",
        epilog="Subcommands: `coded config setup|init|path|show`, `coded models [--check]`, "
               "`coded skill install|list|remove|update|info`, `coded prompts list|show|init`, "
               "`coded plugin install|list|remove|update|enable|disable|info|search|marketplace`, "
               "`coded review`, `coded commit`, `coded lab`, `coded index`, `coded sessions`, "
               "`coded doctor`, "
               "`coded import` (MCP servers from other agents), "
               "`coded acp` (Agent Client Protocol server), `coded mcp-serve` (MCP server).",
    )
    p.add_argument("prompt", nargs="*", help="Initial request. If omitted, starts the interactive REPL.")
    p.add_argument("-m", "--model", help="Model alias to use (from config).")
    p.add_argument("-p", "--print", action="store_true", dest="print_mode",
                   help="Non-interactive: run the prompt once, print the result, and exit.")
    p.add_argument("--yolo", "--auto-approve", action="store_true", dest="auto_approve",
                   help="Auto-approve all tool actions (no permission prompts). Use with care.")
    p.add_argument("--trust-project", action="store_true",
                   help="Apply this folder's .coded/config.json hooks, MCP servers, endpoints "
                        "and permission settings without asking (also CODED_TRUST_PROJECT=1).")
    p.add_argument("--add-dir", action="append", metavar="DIR", dest="add_dirs",
                   help="Another folder the agent may work in (repeatable).")
    p.add_argument("--accept-edits", action="store_true",
                   help="File edits run without asking (shell commands and deletes still ask).")
    p.add_argument("--max-cost", type=float, metavar="DOLLARS",
                   help="Stop once the session's estimated cost reaches this many dollars.")
    p.add_argument("--no-stream", action="store_true", help="Disable streaming output.")
    p.add_argument("--no-compact", action="store_true", help="Disable automatic context compaction.")
    p.add_argument("--cwd", help="Working directory for the agent (default: current dir).")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--system", help="Extra system-prompt instructions to append.")
    p.add_argument("--max-turns", type=int, help="Cap on agent tool-loop iterations.")
    p.add_argument("--tui", action="store_true",
                   help="Launch the full-screen terminal UI (requires: pip install 'coded[tui]').")
    p.add_argument("--no-mcp", action="store_true", help="Disable MCP servers for this run.")
    p.add_argument("--no-skills", action="store_true", help="Disable skill discovery for this run.")
    p.add_argument("--image", action="append", metavar="PATH",
                   help="Attach an image to the initial prompt (repeatable; needs a vision model).")
    p.add_argument("--continue", dest="continue_session", action="store_true",
                   help="Resume the most recent saved session for this directory.")
    p.add_argument("--resume", metavar="ID", help="Resume a specific saved session by id.")
    p.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text",
                   help="Output format for --print mode: json emits content + usage + cost at "
                        "the end; stream-json emits one JSON event per line as the agent works.")
    p.add_argument("--plan", action="store_true",
                   help="Start in plan mode (read-only research until you approve a plan).")
    p.add_argument("--shell", metavar="NAME",
                   help="Windows only: shell for the bash tool (bash, pwsh, powershell, cmd, "
                        "or a path). Default: Git Bash if installed, else PowerShell.")
    p.add_argument("--sandbox", action="store_true",
                   help="Run bash in an OS sandbox (bwrap/sandbox-exec): writes limited to cwd, no network.")
    p.add_argument("--sandbox-network", action="store_true",
                   help="With --sandbox, allow network access.")
    p.add_argument("--trace", metavar="FILE", help="Write JSONL trace spans (turns, LLM calls, tools) to FILE.")
    p.add_argument("--reasoning-effort", choices=["minimal", "low", "medium", "high"],
                   help="Reasoning effort for reasoning models.")
    p.add_argument("--show-reasoning", action="store_true", help="Display streamed reasoning text.")
    p.add_argument("--injection-guard", choices=["off", "warn", "strict"],
                   help="Prompt-injection guard after untrusted content (default: warn).")
    p.add_argument("--version", action="version", version=f"coded {__version__}")

    # Ad-hoc model overrides (no config file needed).
    g = p.add_argument_group("ad-hoc model (overrides config)")
    g.add_argument("--base-url", help="OpenAI-compatible base URL, e.g. http://localhost:11434/v1")
    g.add_argument("--api-key", help="API key for the endpoint.")
    g.add_argument("--model-name", help="Concrete API model id, e.g. gpt-4o or llama-3.3-70b.")
    g.add_argument("--provider", help="Provider preset name (openai, groq, ollama, ...).")

    tls = p.add_argument_group("TLS / certificates")
    tls.add_argument("--ca-bundle", help="Path to a CA cert bundle (PEM) to trust for the endpoint.")
    tls.add_argument("--insecure", action="store_true",
                     help="Disable TLS certificate verification (insecure; self-signed endpoints).")
    return p


def build_config_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coded config", description="Manage configuration.")
    p.add_argument("config_command", choices=["setup", "init", "path", "show"],
                   help="Config action ('setup' is the interactive model wizard).")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Working directory.")
    p.add_argument("--project", action="store_true",
                   help="Edit the project config (./.coded/config.json) instead of the user one.")
    return p


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if getattr(args, "shell", None):
        cfg.shell = args.shell
    if args.auto_approve:
        cfg.auto_approve = True
    if getattr(args, "accept_edits", False):
        cfg.accept_edits = True
    base = getattr(args, "cwd", None) or os.getcwd()
    dirs = list(cfg.add_dirs or []) + list(getattr(args, "add_dirs", None) or [])
    cfg.add_dirs = list(dict.fromkeys(
        os.path.abspath(os.path.join(base, os.path.expanduser(str(d)))) for d in dirs))
    for d in cfg.add_dirs:
        if not os.path.isdir(d):
            raise ConfigError(f"--add-dir: {d} is not a folder.")
    if getattr(args, "max_cost", None) is not None:
        cfg.max_cost = args.max_cost
    if args.no_stream:
        cfg.stream = False
    if args.no_compact:
        cfg.auto_compact = False
    if args.max_turns:
        cfg.max_turns_override = args.max_turns
    if getattr(args, "sandbox", False):
        cfg.sandbox = dict(cfg.sandbox, enabled=True)
    if getattr(args, "sandbox_network", False):
        cfg.sandbox = dict(cfg.sandbox, network=True)
    if getattr(args, "show_reasoning", False):
        cfg.show_reasoning = True
    if getattr(args, "injection_guard", None):
        cfg.injection_guard = args.injection_guard

    # If ad-hoc endpoint flags are given, register a CLI model and make it default.
    if any([args.base_url, args.api_key, args.model_name, args.provider]):
        mc = model_from_overrides(
            model_name=args.model_name,
            provider=args.provider,
            base_url=args.base_url,
            api_key=args.api_key,
        )
        cfg.add_model(mc, make_default=True)
        args.model = args.model or "cli"


def _select_model(cfg: Config, name: Optional[str]) -> tuple:
    """(model, pool name). `name` may be a pool: one member is picked here."""
    wanted = name or cfg.default_model
    if wanted and wanted not in cfg.models and cfg.pool_members(wanted):
        model, pool = cfg.resolve_target(wanted)
        return model, pool
    return cfg.get_model(name), None


def _warn_sandbox(cfg: Config) -> None:
    from coded.sandbox import sandbox_state

    settings, backend = sandbox_state(cfg)
    if settings.enabled and backend is None:
        how = "refusing to run bash" if settings.fail_if_unavailable else \
            "bash will run unsandboxed and ask for permission"
        ui.warn("Sandbox enabled but no backend found (install bubblewrap on Linux; "
                "macOS has sandbox-exec built in; Windows has no backend — use WSL) "
                f"— {how}.")


def _apply_tls_overrides(model: ModelConfig, args: argparse.Namespace) -> None:
    """Apply --ca-bundle / --insecure to the selected model and warn if insecure."""
    if getattr(args, "ca_bundle", None):
        model.ca_bundle = args.ca_bundle
    if getattr(args, "insecure", False):
        model.verify_ssl = False
    if not model.verify_ssl:
        ui.warn(f"TLS certificate verification is DISABLED for model "
                f"'{model.name}' — the connection is insecure.")


def _init_mcp(cfg: Config, disabled: bool):
    """Start MCP servers if configured; return (manager, extra_tools)."""
    if disabled or not cfg.mcp_servers:
        return None, []
    from coded.mcp_client import MCPManager, mcp_available

    if not mcp_available():
        ui.warn("MCP servers are configured but the 'mcp' package is not installed. "
                "Install with: pip install 'coded[mcp]'  (skipping MCP)")
        return None, []
    manager = MCPManager(cfg.mcp_servers)
    manager.start()
    for err in manager.errors:
        ui.warn(err)
    tools = manager.build_tools(manager)
    if tools:
        ui.info(f"Loaded {len(tools)} MCP tool(s) from {len(manager.sessions)} server(s).")
    return manager, tools


def _handle_config_command(args: argparse.Namespace) -> int:
    if args.config_command == "setup":
        from coded.config_wizard import run_wizard

        return run_wizard(project=args.project, config_path=args.config, cwd=args.cwd)
    if args.config_command == "init":
        try:
            path = write_sample_config()
        except ConfigError as exc:
            ui.error(str(exc))
            return 1
        ui.success(f"Wrote sample config to {path}")
        ui.info("Edit it to add your models and API keys, "
                "or run `coded config setup` to do it interactively.")
        return 0
    if args.config_command == "path":
        from coded.config import user_config_path

        print(user_config_path())
        return 0
    if args.config_command == "show":
        cfg = load_config(cwd=args.cwd, config_path=args.config)
        ui.info(f"Default model: {cfg.default_model}")
        ui.info(f"Models: {', '.join(cfg.models) or '(none)'}")
        ui.info(f"MCP servers: {', '.join(cfg.mcp_servers) or '(none)'}")
        ui.info(f"auto_approve={cfg.auto_approve}  max_turns={cfg.max_turns}  stream={cfg.stream}")
        ui.info(f"Sources: {', '.join(cfg.source_paths) or '(defaults/env)'}")
        return 0
    ui.error("Unknown config subcommand. Try: coded config setup|init|path|show")
    return 1


def _handle_agents_command(argv: List[str]) -> int:
    """`coded agents`: which outside agent CLIs coded can delegate to, and the
    install command for the rest. Nothing is ever installed from here."""
    import json as _json

    from coded.external_agents import agent_statuses, version_of

    p = argparse.ArgumentParser(
        prog="coded agents",
        description="Show which outside coding agents (Aider, OpenCode, Goose, Codex, Gemini "
                    "CLI, …) are installed for the external_agent tool, and how to install "
                    "the others. Install commands are printed for you to run, never run.")
    p.add_argument("names", nargs="*", help="Only these agents.")
    p.add_argument("--check", action="store_true",
                   help="Start each installed agent with --version to prove it runs.")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Working directory.")
    args = p.parse_args(argv)
    cfg = load_config(cwd=args.cwd, config_path=args.config)
    statuses = agent_statuses(cfg)
    if args.names:
        unknown = [n for n in args.names if n not in {s.name for s in statuses}]
        if unknown:
            ui.error(f"Unknown agent(s): {', '.join(unknown)}. Known: "
                     + ", ".join(s.name for s in statuses))
            return 2
        statuses = [s for s in statuses if s.name in args.names]
    checks = {}
    if args.check:
        for s in statuses:
            if s.path and s.state in ("ready", "detection off"):
                checks[s.name] = version_of(s.path)

    if args.json:
        rows = []
        for s in statuses:
            row = {k: v for k, v in s.__dict__.items()}
            if s.name in checks:
                row["check"] = {"ok": checks[s.name][0], "output": checks[s.name][1]}
            rows.append(row)
        print(_json.dumps(rows, indent=2))
        return 0 if all(c[0] for c in checks.values()) else 1

    from rich.table import Table

    table = Table(title="Outside agents (delegated to with the external_agent tool)",
                  title_style=f"bold {ui.ACCENT}", show_lines=False)
    table.add_column("agent", style="bold")
    table.add_column("status")
    table.add_column("open source", justify="center")
    table.add_column("details", overflow="fold")
    marks = {"ready": f"[{ui.ACCENT2}]✓ ready[/]", "not installed": f"[{ui.MUTED}]– not installed[/]",
             "disabled": f"[{ui.WARN}]disabled in config[/]",
             "detection off": f"[{ui.WARN}]installed, detection off[/]"}
    missing = []
    for s in statuses:
        oss = "" if s.open_source is None else ("yes" if s.open_source else "no")
        if s.state == "not installed":
            detail = s.note or ""
            if s.install:
                missing.append(s)
                detail = "see below" + (f" — {s.note}" if s.note else "")
        else:
            detail = s.path or ""
            if s.name in checks:
                ok, out = checks[s.name]
                detail += f"\n{'✓' if ok else '✗'} {out}"
            if s.state == "detection off":
                detail += "\nadd it under `external_agents` to offer it"
        table.add_row(s.name + (" *" if s.configured else ""), marks.get(s.state, s.state),
                      oss, detail)
    ui.console.print(table)
    if any(s.configured for s in statuses):
        ui.console.print(f"[{ui.MUTED}]* defined or changed in your config[/]")
    if missing:
        ui.console.print(f"\n[bold {ui.ACCENT}]To install[/] [{ui.MUTED}](run these yourself; "
                         "check each project's page first):[/]")
        for s in missing:
            ui.console.print(f"  [bold]{s.name}[/]  [{ui.MUTED}]{s.homepage}[/]")
            ui.console.print(f"    {s.install}", markup=False, highlight=False, soft_wrap=True)
            if s.note:
                ui.console.print(f"    [{ui.WARN}]{s.note}[/]")
        ui.console.print(f"[{ui.MUTED}]Once installed, coded finds it on PATH automatically "
                         "(start a new session). Point an open-source agent at your own models "
                         "with `env` under `external_agents` in config.json.[/]")
    if checks and not all(c[0] for c in checks.values()):
        return 1
    return 0


def _handle_models_command(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="coded models", description="List configured models.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Working directory.")
    p.add_argument("--check", nargs="*", metavar="ALIAS",
                   help="Send a tiny request to each model (or the named ones) and report status.")
    p.add_argument("--speed", nargs="*", metavar="ALIAS",
                   help="Time each model (or the named ones) on the request a real turn sends: "
                        "time to first token, total, tokens/s.")
    p.add_argument("--runs", type=int, default=1, help="With --speed: requests per model "
                   "(the median is shown).")
    p.add_argument("--minimal", action="store_true",
                   help="With --speed: a bare question, without coded's system prompt and tools.")
    args = p.parse_args(argv)
    cfg = load_config(cwd=args.cwd, config_path=args.config)
    if not cfg.models:
        ui.info("No models configured. Run `coded config setup` to add one, or set OPENAI_API_KEY.")
        return 0
    roles = {}
    for role, alias in cfg.model_roles.items():
        roles.setdefault(alias, []).append(role)
    if args.speed is not None:
        return _speed_models(cfg, args.speed or list(cfg.models), args.runs, args.minimal,
                             args.cwd or os.getcwd())
    if args.check is None:
        for name, mc in cfg.models.items():
            marker = "*" if name == cfg.default_model else " "
            extra = []
            from coded.prompt_files import prompt_profile

            extra.append(f"{mc.context_window // 1000}k ctx, {prompt_profile(mc)} prompts")
            if mc.fallback:
                extra.append("fallback→" + ",".join(mc.fallback))
            if name in roles:
                extra.append("roles: " + ",".join(roles[name]))
            try:
                url = mc.resolved_base_url()
            except ConfigError:
                url = "(no base_url)"
            print(f"{marker} {name:<16} {mc.provider:<12} {mc.model:<32} {url}"
                  + (f"   [{'; '.join(extra)}]" if extra else ""))
        for name, members in (cfg.pools or {}).items():
            picked = cfg.pool_members(name)
            print(f"  {name:<16} pool         {', '.join(picked) or '(no valid members)'}")
        if not cfg.auto_fallback:
            print("  auto-fallback   off          a failing model will not try the others")
        routing = cfg.routing_settings()
        if routing.configured:
            levels = ", ".join(f"{lvl}→{routing.target(lvl)}"
                               for lvl in ("simple", "medium", "hard") if routing.target(lvl))
            print(f"  routing          {'on ' if routing.enabled else 'off'}          {levels}")
        for w in cfg.model_warnings():
            ui.warn(w)
        return 0
    wanted = args.check or list(cfg.models)
    expanded: List[str] = []
    for name in wanted:  # checking a pool checks every member
        members = cfg.pool_members(name)
        expanded += members or [name]
    return _check_models(cfg, list(dict.fromkeys(expanded)))


def _speed_models(cfg: Config, names: List[str], runs: int, minimal: bool, cwd: str) -> int:
    from rich.table import Table

    from coded.speed import advice, measure, request_for

    expanded: List[str] = []
    for name in names:
        expanded += cfg.pool_members(name) or [name]
    unknown = [n for n in expanded if n not in cfg.models]
    if unknown:
        ui.error(f"Unknown model(s): {', '.join(unknown)}. Known: {', '.join(cfg.models)}")
        return 2
    shape = "a bare question" if minimal else "coded's system prompt + tools + a question"
    ui.info(f"Timing {len(expanded)} model(s) on {shape}, {runs} run(s) each…")
    results = []
    for name in dict.fromkeys(expanded):
        model = cfg.models[name]
        with ui.thinking(f"{name}…"):
            try:
                messages, tools = request_for(model, cfg, cwd, minimal)
            except Exception as exc:  # noqa: BLE001
                from coded.speed import SpeedResult

                results.append(SpeedResult(name, False, error=str(exc)))
                continue
            results.append(measure(model, messages, tools, runs))
    table = Table(title_style=f"bold {ui.ACCENT}", title="Model speed (median)")
    for col, justify in (("model", "left"), ("first token", "right"), ("total", "right"),
                         ("speed", "right"), ("prompt", "right"), ("output", "right")):
        table.add_column(col, justify=justify)
    for r in results:
        if not r.ok:
            table.add_row(r.name, f"[{ui.ERR}]failed[/]", "", "", "", r.error[:60])
            continue
        mark = "~" if r.estimated else ""
        table.add_row(r.name, f"{r.first_token:.2f}s", f"{r.total:.2f}s",
                      f"{r.tokens_per_second:.0f} tok/s" if r.tokens_per_second else "–",
                      f"{mark}{r.prompt_tokens:,}", f"{mark}{r.output_tokens:,}")
    ui.console.print(table)
    if any(r.estimated for r in results if r.ok):
        ui.console.print(f"[{ui.MUTED}]~ estimated: the server did not report token usage[/]")
    for line in advice(results):
        ui.console.print(f"[{ui.MUTED}]{line}[/]")
    return 0 if all(r.ok for r in results) else 1


def _check_models(cfg: Config, names: List[str]) -> int:
    """Probe each model with progressively richer requests.

    A single minimal request is not enough: an endpoint that answers "hi" can
    still 500 on streaming, on `stream_options` or on tool definitions, which is
    exactly the request a real turn sends.
    """
    import time

    from coded.llm import probe_hint, probe_stages

    failures = 0
    for name in names:
        if name not in cfg.models:
            print(f"  {name:<16} unknown alias")
            failures += 1
            continue
        mc = cfg.models[name]
        start = time.time()
        try:
            stages = probe_stages(mc)
        except Exception as exc:  # noqa: BLE001 - e.g. a missing key
            failures += 1
            print(f"\u2717 {name:<16} {type(exc).__name__}: {exc}")
            continue
        ms = (time.time() - start) * 1000
        bad = [(label, exc) for label, exc in stages if exc is not None]
        if not bad:
            print(f"\u2713 {name:<16} {ms:7.0f} ms   "
                  f"{len(stages)} request shapes accepted")
            continue
        failures += 1
        print(f"\u2717 {name:<16} {ms:7.0f} ms")
        for label, exc in stages:
            if exc is None:
                print(f"      \u2713 {label}")
            else:
                detail = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
                print(f"      \u2717 {label}: {type(exc).__name__}: {detail}")
                print(f"      \u2192 {probe_hint(label, exc)}")
    return 1 if failures else 0


def _handle_index_command(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="coded index", description="Build the semantic code index.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Project directory to index.")
    p.add_argument("-m", "--model", help="Active model (used to guess the embedding provider).")
    args = p.parse_args(argv)
    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    try:
        cfg = _load_noting(cwd, args.config)
        from coded.embeddings import EmbeddingClient, resolve_embedding_model
        from coded.index import CodeIndex

        active = None
        try:
            active = cfg.get_model(args.model)
        except ConfigError:
            pass
        embed_model = resolve_embedding_model(cfg, active)
        client = EmbeddingClient(embed_model)
        index = CodeIndex(cwd)
        ui.info(f"Indexing {cwd} with embedding model '{embed_model.model}'…")

        def progress(done, total):
            ui.console.print(f"  [dim]embedded {done}/{total} chunks[/dim]", end="\r")

        n = index.build(client, progress=progress)
        ui.console.print()
        ui.success(f"Indexed {n} chunks → {index.path}")
        return 0
    except ConfigError as exc:
        ui.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        ui.error(f"Indexing failed: {exc}")
        return 1


def _handle_commit_command(argv: List[str]) -> int:
    """Generate a Conventional Commits message for the staged changes."""
    p = argparse.ArgumentParser(prog="coded commit",
                                description="Draft a commit message from staged changes.")
    p.add_argument("-m", "--model", help="Model alias to use.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Repository directory.")
    p.add_argument("--commit", action="store_true", help="Actually create the commit.")
    p.add_argument("--base-url", help="Ad-hoc base URL.")
    p.add_argument("--api-key", help="Ad-hoc API key.")
    p.add_argument("--model-name", help="Ad-hoc model id.")
    p.add_argument("--provider", help="Ad-hoc provider.")
    p.add_argument("--ca-bundle", help="CA cert bundle (PEM) to trust.")
    p.add_argument("--insecure", action="store_true", help="Disable TLS verification (insecure).")
    args = p.parse_args(argv)

    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    try:
        cfg = _load_noting(cwd, args.config)
        if any([args.base_url, args.api_key, args.model_name, args.provider]):
            cfg.add_model(model_from_overrides(
                model_name=args.model_name, provider=args.provider,
                base_url=args.base_url, api_key=args.api_key), make_default=True)
            args.model = args.model or "cli"
        model = cfg.get_model(args.model)
    except ConfigError as exc:
        ui.error(str(exc))
        return 2
    _apply_tls_overrides(model, args)

    from coded.skills import discover_skills

    # Reading the diff needs no approval (read-only git); creating the commit
    # is allowed only with --commit, and only for git. The user's deny rules
    # still win over that allow rule.
    rules = {k: list(v) for k, v in (cfg.permissions or {}).items() if isinstance(v, list)}
    if args.commit:
        rules["allow"] = list(rules.get("allow") or []) + ["git"]
    permissions = PermissionManager(rules=rules, injection_guard=cfg.injection_guard)
    agent = create_agent(model=model, config=cfg, cwd=cwd, permissions=permissions,
                         skills=discover_skills(cwd), stream=False, verbose=False)
    action = ("Then create the commit using the git tool."
              if args.commit else "Do NOT create the commit — only output the message.")
    prompt = (
        "Use the conventional-commit skill to draft a Conventional Commits message for the "
        f"currently staged changes (inspect them with `git diff --cached`). {action} "
        "If nothing is staged, say so."
    )
    result = agent.run(prompt)
    if result:
        ui.assistant_markdown(result)
    return 0


def _handle_sessions_command(argv: List[str]) -> int:
    if argv[:1] == ["show"]:
        return _sessions_show(argv[1:])
    p = argparse.ArgumentParser(prog="coded sessions", description="List saved sessions "
                                "(coded sessions show <id> [N] replays one).")
    p.add_argument("--cwd", help="Only sessions for this directory.")
    args = p.parse_args(argv)
    from coded.sessions_store import SessionStore

    metas = SessionStore().list()
    if args.cwd:
        cwd = os.path.abspath(args.cwd)
        metas = [m for m in metas if m.cwd == cwd]
    if not metas:
        ui.info("No saved sessions.")
        return 0
    for m in metas:
        when = _dt.datetime.fromtimestamp(m.updated).strftime("%Y-%m-%d %H:%M")
        print(f"{m.id}  {when}  {m.model:<14} {m.messages:>3} msgs  {m.cwd}")
    ui.info("Resume with: coded --resume <id>  (or --continue for the latest here)")
    return 0


def _handle_lab_command(argv: List[str]) -> int:
    """coded lab [--apply]: recurring failures in this folder's past sessions."""
    from coded import lab

    p = argparse.ArgumentParser(prog="coded lab", description="Look through this folder's "
                                "saved sessions for recurring failures and propose fixes.")
    p.add_argument("--cwd", help="Project directory.")
    p.add_argument("--apply", action="store_true",
                   help="Ask about each proposal and apply the ones you approve (needs a "
                        "terminal; there is deliberately no --yes).")
    args = p.parse_args(argv)
    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    report = lab.analyze(lab.load_sessions(cwd))
    ui.console.print(lab.render(report))
    if args.apply:
        if not _stdin_is_tty():
            ui.error("--apply asks about each change, so it needs a terminal.")
            return 2
        n = lab.apply_interactive(cwd, report, input, lambda m: ui.console.print(m, markup=False))
        ui.info(f"Applied {n} change(s).")
    return 0


def _sessions_show(argv: List[str]) -> int:
    """coded sessions show <id> [N]: a saved session's timeline (view only)."""
    from coded import timeline
    from coded.sessions_store import SessionStore

    if not argv:
        ui.error("Usage: coded sessions show <id> [request number]")
        return 2
    store = SessionStore()
    data = store._read(store.dir / f"{argv[0]}.json")
    session = store.load(argv[0])
    if session is None or data is None:
        ui.error(f"No saved session '{argv[0]}'. List them with: coded sessions")
        return 1
    turns = timeline.build(session.messages, session.events)
    cwd = data.get("cwd") or os.getcwd()
    if len(argv) > 1:
        try:
            turn = turns[int(argv[1]) - 1]
        except (ValueError, IndexError):
            ui.error(f"Choose a request between 1 and {len(turns)}.")
            return 2
        ui.console.print(timeline.render_turn(turn, None, cwd))
        ui.info("(A saved session keeps the conversation; its file states are only in "
                "that project's checkpoints, so files are not shown here.)")
        return 0
    ui.console.print(timeline.render_list(turns))
    return 0


def _handle_review_command(argv: List[str]) -> int:
    """Non-interactive review pipeline for CI: reviewer → qa → security → verdict."""
    p = argparse.ArgumentParser(
        prog="coded review",
        description="Run the reviewer→qa→security pipeline non-interactively (for CI).",
    )
    p.add_argument("target", nargs="*", help="Files/description to review (default: git diff).")
    p.add_argument("-m", "--model", help="Model alias to use.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Project directory.")
    p.add_argument("--yolo", "--auto-approve", action="store_true", dest="auto_approve",
                   help="Auto-approve tool actions so qa/security can run tests and scanners.")
    p.add_argument("--output", "-o", help="Write the aggregated report to this file.")
    p.add_argument("--fail-on", choices=["needs-changes", "never"], default="needs-changes",
                   help="Exit non-zero when the verdict is NEEDS_CHANGES (default) or never.")
    p.add_argument("--no-verdict", action="store_true", help="Skip the pass/fail verdict step.")
    p.add_argument("--adversarial", action="store_true",
                   help="Add a read-only adversarial phase that hunts for security holes, wrong "
                        "assumptions and hidden bugs (findings need a file:line and a trigger).")
    p.add_argument("--base-url", help="Ad-hoc OpenAI-compatible base URL.")
    p.add_argument("--api-key", help="Ad-hoc API key.")
    p.add_argument("--model-name", help="Ad-hoc concrete model id.")
    p.add_argument("--provider", help="Ad-hoc provider preset.")
    p.add_argument("--ca-bundle", help="CA cert bundle (PEM) to trust.")
    p.add_argument("--insecure", action="store_true", help="Disable TLS verification (insecure).")
    args = p.parse_args(argv)

    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    try:
        cfg = _load_noting(cwd, args.config)
        if any([args.base_url, args.api_key, args.model_name, args.provider]):
            cfg.add_model(model_from_overrides(
                model_name=args.model_name, provider=args.provider,
                base_url=args.base_url, api_key=args.api_key), make_default=True)
            args.model = args.model or "cli"
        model = cfg.get_model(args.model)
    except ConfigError as exc:
        ui.error(str(exc))
        return 2
    _apply_tls_overrides(model, args)

    from coded.review import NEEDS_CHANGES, UNKNOWN, run_review, synthesize_verdict
    from coded.skills import discover_skills

    permissions = PermissionManager(auto_approve=args.auto_approve, rules=cfg.permissions,
                                    injection_guard=cfg.injection_guard)
    skills = discover_skills(cwd)
    agent = create_agent(
        model=model, config=cfg, cwd=cwd, permissions=permissions,
        skills=skills, stream=False, verbose=False,
    )

    target = " ".join(args.target).strip() or None
    ui.info("Running review pipeline (reviewer → qa → security)…")
    from coded.review import REVIEW_PHASES

    phases = REVIEW_PHASES + (["adversary"] if args.adversarial else [])
    report = run_review(agent, target, phases=phases, verbose=False)

    verdict = None
    if not args.no_verdict:
        verdict, vtext = synthesize_verdict(agent, report)
        report += f"\n\n## verdict\n\n{vtext}\n"

    print(report)
    if args.output:
        try:
            Path(args.output).write_text(report, encoding="utf-8")
            ui.info(f"Wrote report to {args.output}")
        except OSError as exc:
            ui.error(f"Could not write {args.output}: {exc}")

    if verdict:
        ui.console.print(f"\n[bold]Verdict:[/bold] {verdict}")
        if verdict == NEEDS_CHANGES and args.fail_on == "needs-changes":
            return 1
        if verdict == UNKNOWN:
            ui.warn("Verdict indeterminate; not failing the build.")
    return 0


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:  # noqa: BLE001
        return False


def _ask_project_trust(info: dict, read=input) -> bool:
    """Show what this folder's .coded/ settings would run or change, and ask
    once. A yes is remembered until those settings change."""
    from rich.panel import Panel
    from rich.text import Text

    from coded import trust

    body = Text()
    body.append(f"{info['root']}\n\n", style="bold")
    body.append("This folder's .coded/ settings would:\n", style=ui.MUTED)
    for line in info["lines"]:
        body.append(f"  • {line}\n")
    body.append("\nOnly trust a project whose files you have read or wrote yourself.",
                style=ui.WARN)
    ui.console.print(Panel(body, title=f"[{ui.WARN}]Trust this project?[/]",
                           title_align="left", border_style=ui.WARN))
    try:
        answer = read("  Apply these settings? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in ("y", "yes"):
        trust.remember(info["root"], info["fingerprint"])
        return True
    return False


def _load_noting(cwd: str, config_path: Optional[str]):
    """load_config for a non-interactive subcommand: an untrusted project's
    sensitive settings stay off, with a note on stderr."""
    cfg = load_config(cwd=cwd, config_path=config_path)
    if cfg.untrusted_project:
        _note_untrusted(cfg.untrusted_project)
    return cfg


def _note_untrusted(info: dict, asked: bool = False) -> None:
    """Say (on stderr, so piped output stays clean) what was not applied."""
    head = ("Not applying" if asked else "Untrusted project: not applying")
    print(f"coded: {head} {len(info['lines'])} setting(s) from "
          f"{os.path.join(info['root'], '.coded')} (hooks, MCP servers, endpoints or "
          "permissions). Run coded in this folder interactively to review them, or pass "
          "--trust-project / set CODED_TRUST_PROJECT=1.", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    os.environ.setdefault("CODED_DATE", _dt.date.today().isoformat())
    configure_stdio()  # legacy Windows code pages can't print the UI
    if argv is None:
        argv = sys.argv[1:]

    # Dispatch git-style subcommands by the first token.
    if argv and argv[0] == "config":
        cfg_args = build_config_parser().parse_args(argv[1:])
        return _handle_config_command(cfg_args)
    if argv and argv[0] == "models":
        return _handle_models_command(argv[1:])
    if argv and argv[0] == "agents":
        return _handle_agents_command(argv[1:])
    if argv and argv[0] == "doctor":
        from coded.doctor import main as doctor_main

        return doctor_main(argv[1:])
    if argv and argv[0] == "index":
        return _handle_index_command(argv[1:])
    if argv and argv[0] == "review":
        return _handle_review_command(argv[1:])
    if argv and argv[0] == "sessions":
        return _handle_sessions_command(argv[1:])
    if argv and argv[0] == "commit":
        return _handle_commit_command(argv[1:])
    if argv and argv[0] == "lab":
        return _handle_lab_command(argv[1:])
    if argv and argv[0] == "skill":
        from coded.manage_cli import skill_main

        return skill_main(argv[1:])
    if argv and argv[0] == "plugin":
        from coded.manage_cli import plugin_main

        return plugin_main(argv[1:])
    if argv and argv[0] == "import":
        from coded.agent_import import main as import_main

        return import_main(argv[1:])
    if argv and argv[0] == "prompts":
        from coded.prompt_files import prompts_main

        return prompts_main(argv[1:])
    if argv and argv[0] == "acp":
        from coded.acp import main as acp_main

        return acp_main(argv[1:])
    if argv and argv[0] == "mcp-serve":
        from coded.mcp_server import main as mcp_main

        return mcp_main(argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)
    # Load the model SDK while config, MCP and the prompt are being set up (and
    # while the user types the first message). `--version` has exited by now.
    from coded.llm import prewarm

    prewarm()

    # In JSON print mode, keep stdout clean — send all log output to stderr.
    if args.print_mode and args.output_format in ("json", "stream-json"):
        ui.set_quiet(True)

    if args.shell:  # before load_config: plugin hooks and prompts need the shell
        set_active_shell(resolve_shell(args.shell), pinned=True)

    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()

    try:
        cfg = load_config(cwd=cwd, config_path=args.config,
                          trust_project=True if args.trust_project else None)
        if cfg.untrusted_project:
            interactive = not args.print_mode and _stdin_is_tty()
            if interactive and _ask_project_trust(cfg.untrusted_project):
                cfg = load_config(cwd=cwd, config_path=args.config, trust_project=True)
            else:
                _note_untrusted(cfg.untrusted_project, asked=interactive)
        _apply_overrides(cfg, args)
    except ConfigError as exc:
        ui.error(str(exc))
        return 1

    try:
        model, model_pool = _select_model(cfg, args.model)
    except ConfigError as exc:
        ui.error(str(exc))
        ui.info("Tip: `coded config setup` configures models interactively, "
                "`coded config init` writes a sample, or pass "
                "--base-url/--api-key/--model-name for a one-off endpoint.")
        return 1

    _apply_tls_overrides(model, args)
    if args.reasoning_effort:
        model.reasoning_effort = args.reasoning_effort
    _warn_sandbox(cfg)
    from coded.limits import LIMIT_KEYS, unknown_limit_keys

    for mc in cfg.models.values():
        bad = unknown_limit_keys(mc)
        if bad:
            ui.warn(f"Model '{mc.name}': unknown limits key(s) {', '.join(bad)} (ignored). "
                    f"Valid keys: {', '.join(LIMIT_KEYS)}")
    for w in cfg.model_warnings():
        ui.warn(w)
    for w in cfg.plugin_warnings:
        ui.warn(w)
    if cfg.plugins:
        ui.info(f"Plugins: {', '.join(cfg.plugins)}")

    permissions = PermissionManager(auto_approve=cfg.auto_approve, rules=cfg.permissions,
                                    injection_guard=cfg.injection_guard)
    mcp_manager, extra_tools = _init_mcp(cfg, disabled=args.no_mcp)

    skills = {}
    if not args.no_skills:
        from coded.skills import discover_skills

        skills = discover_skills(cwd)
        if skills:
            ui.info(f"Loaded {len(skills)} skill(s): {', '.join(skills)}")

    agent = create_agent(
        model=model,
        config=cfg,
        cwd=cwd,
        permissions=permissions,
        system_extra=args.system,
        extra_tools=extra_tools,
        skills=skills,
        stream=cfg.stream,
        verbose=True,
        tracer=tracer_from_config(cfg, args.trace),
        pool=model_pool,
    )
    if args.model:
        agent.set_routing(False)  # an explicit -m pins the model
    if args.plan:
        agent.set_mode("plan", announce=False)

    # Session persistence: resume a saved session, or start a fresh id.
    from coded.sessions_store import SessionStore

    store = SessionStore()
    resume_id = args.resume or (store.latest(cwd) if args.continue_session else None)
    if resume_id:
        loaded = store.load(resume_id)
        if loaded is not None:
            agent.session = loaded
            agent.todos.items = [dict(t) for t in loaded.todos]
            session_id = resume_id
            ui.info(f"Resumed session {resume_id} ({len(loaded.messages)} messages).")
        else:
            ui.warn(f"No saved session '{resume_id}'; starting a new one.")
            session_id = store.new_id()
    else:
        session_id = store.new_id()

    initial_prompt = " ".join(args.prompt) if args.prompt else None
    images = args.image or None
    if images and not model.supports_vision:
        ui.warn(f"Model '{model.name}' is not marked supports_vision; images may be ignored.")

    def autosave():
        agent.session.todos = [dict(t) for t in agent.todos.items]
        try:
            store.save(agent.session, session_id=session_id, cwd=cwd, model=model.name)
        except OSError:
            pass

    agent.on_step = autosave
    try:
        if args.print_mode:
            return _run_print_mode(agent, initial_prompt, images,
                                   output_format=args.output_format, on_done=autosave)
        if args.tui:
            try:
                from coded.tui import run_tui
            except ImportError:
                ui.error("The TUI needs Textual. Install it with: pip install 'coded[tui]'")
                return 1
            run_tui(agent, on_turn=autosave)
            autosave()
            return 0
        from coded.repl import Repl

        try:
            repl = Repl(agent, cfg, on_turn=autosave)
        except ConfigError as exc:
            ui.error(str(exc))
            return 1
        repl.run(initial=initial_prompt, images=images)
        autosave()
        return 0
    finally:
        if mcp_manager is not None:
            mcp_manager.stop()


def _run_print_mode(agent, prompt: Optional[str], images=None, *,
                    output_format: str = "text", on_done=None) -> int:
    if not prompt:
        # Read the prompt from stdin when piped.
        prompt = sys.stdin.read().strip()
    if not prompt:
        ui.error("No prompt provided for --print mode.")
        return 1
    import json as _json

    def emit(event: dict) -> None:
        print(_json.dumps(event, default=str, ensure_ascii=False), flush=True)

    if output_format == "json":
        # Keep stdout clean for machine parsing: no streaming/tool chatter.
        agent.stream = False
        agent.verbose = False
    elif output_format == "stream-json":
        # One JSON object per line: the agent's own events (assistant_delta,
        # tool_call, tool_result, diff, todos, status…), then a "result".
        agent.verbose = False
        agent.on_event = emit
        emit({"type": "start", "model": agent.model.name, "cwd": agent.cwd})
    # In print mode we auto-approve nothing by default; keep it non-interactive.
    try:
        result = agent.run(prompt, images=images)
    except Exception as exc:  # noqa: BLE001 - report it readably, don't traceback
        from coded.llm import explain_failure

        ui.error(explain_failure(exc))
        if output_format == "stream-json":
            emit({"type": "error", "message": explain_failure(exc)})
        return 1
    if on_done:
        on_done()
    if output_format == "stream-json":
        u = agent.session.total_usage
        emit({"type": "result", "content": result, "model": agent.model.name,
              "usage": {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens,
                        "cached_input_tokens": u.cached_tokens},
              "cost": round(agent.session.estimated_cost(agent.model), 6)})
    elif output_format == "json":
        u = agent.session.total_usage
        print(_json.dumps({
            "content": result,
            "model": agent.model.name,
            "usage": {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens,
                      "cached_input_tokens": u.cached_tokens},
            "cost": round(agent.session.estimated_cost(agent.model), 6),
        }, indent=2))
    elif result and not agent.stream and not agent.verbose:
        ui.assistant_markdown(result)  # a verbose non-streaming loop already printed it
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
