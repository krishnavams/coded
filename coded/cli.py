"""Command-line entry point for coded."""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
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
    p.add_argument("--cwd", help="Working directory for the agent (default: current dir).")
    p.add_argument("--config", help="Path to a specific config file.")
    p.add_argument("--system", help="Extra system-prompt instructions to append.")
    p.add_argument("--max-turns", type=int, help="Cap on agent tool-loop iterations.")
    p.add_argument("--no-mcp", action="store_true", help="Disable MCP servers for this run.")
    p.add_argument("--no-skills", action="store_true", help="Disable skill discovery for this run.")
    p.add_argument("--image", action="append", metavar="PATH",
                   help="Attach an image to the initial prompt (repeatable; needs a vision model).")
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

    parser = build_parser()
    args = parser.parse_args(argv)

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

    permissions = PermissionManager(auto_approve=cfg.auto_approve)
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

    initial_prompt = " ".join(args.prompt) if args.prompt else None
    images = args.image or None
    if images and not model.supports_vision:
        ui.warn(f"Model '{model.name}' is not marked supports_vision; images may be ignored.")

    try:
        if args.print_mode:
            return _run_print_mode(agent, initial_prompt, images)
        from coded.repl import Repl

        Repl(agent, cfg).run(initial=initial_prompt, images=images)
        return 0
    finally:
        if mcp_manager is not None:
            mcp_manager.stop()


def _run_print_mode(agent, prompt: Optional[str], images=None) -> int:
    if not prompt:
        # Read the prompt from stdin when piped.
        prompt = sys.stdin.read().strip()
    if not prompt:
        ui.error("No prompt provided for --print mode.")
        return 1
    # In print mode we auto-approve nothing by default; keep it non-interactive.
    result = agent.run(prompt, images=images)
    if result and not agent.stream:
        ui.assistant_markdown(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
