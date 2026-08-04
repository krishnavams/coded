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


SUBCOMMANDS = {"config", "models"}


def build_parser() -> argparse.ArgumentParser:
    """Parser for the default `run` behaviour (and `models`, which needs no args).

    `config` is dispatched separately (see build_config_parser) because mixing a
    free-form positional prompt with argparse subparsers is unreliable.
    """
    p = argparse.ArgumentParser(
        prog="coded",
        description="A Claude Code-style coding agent for OpenAI-compatible models.",
        epilog="Subcommands: `coded config init|path|show`, `coded models`.",
    )
    p.add_argument("prompt", nargs="*", help="Initial request. If omitted, starts the interactive REPL.")
    p.add_argument("-m", "--model", help="Model alias to use (from config).")
    p.add_argument("-p", "--print", action="store_true", dest="print_mode",
                   help="Non-interactive: run the prompt once, print the result, and exit.")
    p.add_argument("--yolo", "--auto-approve", action="store_true", dest="auto_approve",
                   help="Auto-approve all tool actions (no permission prompts). Use with care.")
    p.add_argument("--no-stream", action="store_true", help="Disable streaming output.")
    p.add_argument("--no-compact", action="store_true", help="Disable automatic context compaction.")
    p.add_argument("--cwd", help="Working directory for the agent (default: current dir).")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--system", help="Extra system-prompt instructions to append.")
    p.add_argument("--max-turns", type=int, help="Cap on agent tool-loop iterations.")
    p.add_argument("--no-mcp", action="store_true", help="Disable MCP servers for this run.")
    p.add_argument("--no-skills", action="store_true", help="Disable skill discovery for this run.")
    p.add_argument("--image", action="append", metavar="PATH",
                   help="Attach an image to the initial prompt (repeatable; needs a vision model).")
    p.add_argument("--continue", dest="continue_session", action="store_true",
                   help="Resume the most recent saved session for this directory.")
    p.add_argument("--resume", metavar="ID", help="Resume a specific saved session by id.")
    p.add_argument("--output-format", choices=["text", "json"], default="text",
                   help="Output format for --print mode (json emits content + usage + cost).")
    p.add_argument("--version", action="version", version=f"coded {__version__}")

    # Ad-hoc model overrides (no config file needed).
    g = p.add_argument_group("ad-hoc model (overrides config)")
    g.add_argument("--base-url", help="OpenAI-compatible base URL, e.g. http://localhost:11434/v1")
    g.add_argument("--api-key", help="API key for the endpoint.")
    g.add_argument("--model-name", help="Concrete API model id, e.g. gpt-4o or llama-3.3-70b.")
    g.add_argument("--provider", help="Provider preset name (openai, groq, ollama, ...).")
    return p


def build_config_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coded config", description="Manage configuration.")
    p.add_argument("config_command", choices=["init", "path", "show"], help="Config action.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Working directory.")
    return p


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if args.auto_approve:
        cfg.auto_approve = True
    if args.no_stream:
        cfg.stream = False
    if args.no_compact:
        cfg.auto_compact = False
    if args.max_turns:
        cfg.max_turns = args.max_turns

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


def _select_model(cfg: Config, name: Optional[str]) -> ModelConfig:
    return cfg.get_model(name)


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
    if args.config_command == "init":
        try:
            path = write_sample_config()
        except ConfigError as exc:
            ui.error(str(exc))
            return 1
        ui.success(f"Wrote sample config to {path}")
        ui.info("Edit it to add your models and API keys.")
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
    ui.error("Unknown config subcommand. Try: coded config init")
    return 1


def _handle_models_command(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="coded models", description="List configured models.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Working directory.")
    args = p.parse_args(argv)
    cfg = load_config(cwd=args.cwd, config_path=args.config)
    if not cfg.models:
        ui.info("No models configured. Run `coded config init` or set OPENAI_API_KEY.")
        return 0
    for name, mc in cfg.models.items():
        marker = "*" if name == cfg.default_model else " "
        print(f"{marker} {name:<16} {mc.provider:<12} {mc.model}")
    return 0


def _handle_index_command(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="coded index", description="Build the semantic code index.")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--cwd", help="Project directory to index.")
    p.add_argument("-m", "--model", help="Active model (used to guess the embedding provider).")
    args = p.parse_args(argv)
    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    try:
        cfg = load_config(cwd=cwd, config_path=args.config)
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
    args = p.parse_args(argv)

    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    try:
        cfg = load_config(cwd=cwd, config_path=args.config)
        if any([args.base_url, args.api_key, args.model_name, args.provider]):
            cfg.add_model(model_from_overrides(
                model_name=args.model_name, provider=args.provider,
                base_url=args.base_url, api_key=args.api_key), make_default=True)
            args.model = args.model or "cli"
        model = cfg.get_model(args.model)
    except ConfigError as exc:
        ui.error(str(exc))
        return 2

    from coded.skills import discover_skills

    # Allow git so the agent can read the diff (and commit if requested).
    permissions = PermissionManager(auto_approve=True)
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
    p = argparse.ArgumentParser(prog="coded sessions", description="List saved sessions.")
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
    p.add_argument("--base-url", help="Ad-hoc OpenAI-compatible base URL.")
    p.add_argument("--api-key", help="Ad-hoc API key.")
    p.add_argument("--model-name", help="Ad-hoc concrete model id.")
    p.add_argument("--provider", help="Ad-hoc provider preset.")
    args = p.parse_args(argv)

    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()
    try:
        cfg = load_config(cwd=cwd, config_path=args.config)
        if any([args.base_url, args.api_key, args.model_name, args.provider]):
            cfg.add_model(model_from_overrides(
                model_name=args.model_name, provider=args.provider,
                base_url=args.base_url, api_key=args.api_key), make_default=True)
            args.model = args.model or "cli"
        model = cfg.get_model(args.model)
    except ConfigError as exc:
        ui.error(str(exc))
        return 2

    from coded.review import NEEDS_CHANGES, UNKNOWN, run_review, synthesize_verdict
    from coded.skills import discover_skills

    permissions = PermissionManager(auto_approve=args.auto_approve, rules=cfg.permissions)
    skills = discover_skills(cwd)
    agent = create_agent(
        model=model, config=cfg, cwd=cwd, permissions=permissions,
        skills=skills, stream=False, verbose=False,
    )

    target = " ".join(args.target).strip() or None
    ui.info("Running review pipeline (reviewer → qa → security)…")
    report = run_review(agent, target, verbose=False)

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


def main(argv: Optional[List[str]] = None) -> int:
    os.environ.setdefault("CODED_DATE", _dt.date.today().isoformat())
    if argv is None:
        argv = sys.argv[1:]

    # Dispatch git-style subcommands by the first token.
    if argv and argv[0] == "config":
        cfg_args = build_config_parser().parse_args(argv[1:])
        return _handle_config_command(cfg_args)
    if argv and argv[0] == "models":
        return _handle_models_command(argv[1:])
    if argv and argv[0] == "index":
        return _handle_index_command(argv[1:])
    if argv and argv[0] == "review":
        return _handle_review_command(argv[1:])
    if argv and argv[0] == "sessions":
        return _handle_sessions_command(argv[1:])
    if argv and argv[0] == "commit":
        return _handle_commit_command(argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)

    # In JSON print mode, keep stdout clean — send all log output to stderr.
    if args.print_mode and args.output_format == "json":
        ui.set_quiet(True)

    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()

    try:
        cfg = load_config(cwd=cwd, config_path=args.config)
        _apply_overrides(cfg, args)
    except ConfigError as exc:
        ui.error(str(exc))
        return 1

    try:
        model = _select_model(cfg, args.model)
    except ConfigError as exc:
        ui.error(str(exc))
        ui.info("Tip: `coded config init` to create a config, or pass "
                "--base-url/--api-key/--model-name for a one-off endpoint.")
        return 1

    permissions = PermissionManager(auto_approve=cfg.auto_approve, rules=cfg.permissions)
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
    )

    # Session persistence: resume a saved session, or start a fresh id.
    from coded.sessions_store import SessionStore

    store = SessionStore()
    resume_id = args.resume or (store.latest(cwd) if args.continue_session else None)
    if resume_id:
        loaded = store.load(resume_id)
        if loaded is not None:
            agent.session = loaded
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
        try:
            store.save(agent.session, session_id=session_id, cwd=cwd, model=model.name)
        except OSError:
            pass

    try:
        if args.print_mode:
            return _run_print_mode(agent, initial_prompt, images,
                                   output_format=args.output_format, on_done=autosave)
        from coded.repl import Repl

        Repl(agent, cfg, on_turn=autosave).run(initial=initial_prompt, images=images)
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
    if output_format == "json":
        # Keep stdout clean for machine parsing: no streaming/tool chatter.
        agent.stream = False
        agent.verbose = False
    # In print mode we auto-approve nothing by default; keep it non-interactive.
    result = agent.run(prompt, images=images)
    if on_done:
        on_done()
    if output_format == "json":
        import json as _json

        u = agent.session.total_usage
        print(_json.dumps({
            "content": result,
            "model": agent.model.name,
            "usage": {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens},
            "cost": round(agent.session.estimated_cost(agent.model), 6),
        }, indent=2))
    elif result and not agent.stream:
        ui.assistant_markdown(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
