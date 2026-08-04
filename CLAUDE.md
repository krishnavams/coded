# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

## Project overview

**coded** is a Claude Code-style terminal coding agent, written in Python, that
works with any **OpenAI-compatible** chat-completions endpoint. It runs an
agentic tool loop (read/write/edit files, search, run shell commands, spawn
sub-agents) in an interactive REPL, supports multiple configurable models, a
permission layer for risky actions, and an optional MCP client. See
[`README.md`](./README.md) for user-facing docs.

## Tech stack

- **Language**: Python 3.10+
- **LLM access**: [`openai`](https://pypi.org/project/openai/) SDK, pointed at
  any OpenAI-compatible `base_url`. There is no hard dependency on OpenAI the
  company — the same client talks to Groq, Ollama, llama.cpp, etc.
- **Terminal UI**: [`rich`](https://pypi.org/project/rich/) (Markdown rendering,
  tables, live streaming) and [`prompt_toolkit`](https://pypi.org/project/prompt-toolkit/)
  (REPL input, history, completion).
- **MCP** (optional): [`mcp`](https://pypi.org/project/mcp/) — guarded import;
  the app runs fine without it.
- **Tests**: `pytest`.
- Packaging via `pyproject.toml` (setuptools). Console script: `coded`.

## Repository structure

```
coded/
├── __init__.py         # version
├── __main__.py         # `python -m coded`
├── cli.py              # argparse entry point, subcommand dispatch, wiring
├── config.py           # Config/ModelConfig, provider presets, layered loading
├── llm.py              # LLMClient: OpenAI-compatible streaming + tool calls
├── agent.py            # Agent: the agentic loop; sub-agent spawning
├── session.py          # Session: message history + usage/cost tracking
├── permissions.py      # PermissionManager: approve/deny/always gate
├── prompts.py          # system prompt construction (+ project CODED.md/CLAUDE.md)
├── repl.py             # interactive REPL and slash commands
├── ui.py               # rich-based rendering helpers (single Console)
├── skills.py           # skill discovery, frontmatter parsing, prompt section
├── review.py           # /review pipeline: reviewer→qa→security sub-agents
├── builtin_skills/     # bundled agent roles: planner, reviewer, qa, security
├── compaction.py       # context auto-compaction (summarize old messages)
├── sessions_store.py   # persist/restore sessions (--continue/--resume)
├── checkpoints.py      # CheckpointManager: per-turn file snapshots for /undo
├── embeddings.py       # EmbeddingClient + embedding-model resolution
├── index.py            # CodeIndex: chunk/embed/store + cosine search
├── websearch.py        # web-search backends + HTML→text fetch
├── images.py           # image → base64 data URL for vision models
├── mcp_client.py       # optional MCP stdio client (background asyncio loop)
└── tools/
    ├── __init__.py     # build_registry(): assembles the tool suite
    ├── base.py         # Tool, ToolResult, ToolContext, ToolRegistry
    ├── files.py        # read, write, edit, ls, move, delete
    ├── search.py       # glob, grep
    ├── semantic_search.py  # semantic_search (uses the embedded index)
    ├── shell.py        # bash
    ├── git.py          # git (read-only free, mutations gated)
    ├── github.py       # github (PR list/get/create, comment) via GITHUB_TOKEN
    ├── web.py          # web_search, web_fetch
    ├── task.py         # task (sub-agent delegation)
    ├── skill.py        # skill (loads a SKILL.md body into context)
    └── mcp_tool.py     # wraps an MCP server tool as a coded Tool
tests/
├── conftest.py         # FakeServer + EmbeddingsServer + RouteServer (all offline)
├── test_agent_e2e.py   # full agent loop (tool call, streaming, permission)
├── test_review.py      # /review pipeline + coded review verdict/exit code
├── test_compaction.py  # context compaction trigger + tail preservation
├── test_permissions_sessions.py  # allow/deny rules, diff preview, sessions, json
├── test_checkpoints.py # checkpoint/undo + move/delete
├── test_git_tools.py   # git tool + github tool (fake API)
├── test_semantic_and_web.py  # index/search, web_fetch/search, image encoding
├── test_skills.py      # skill discovery, frontmatter, skill tool
└── test_tools.py       # tool + config unit tests
examples/skills/        # example SKILL.md packs + install guide
config.example.json     # sample configuration
```

## How it fits together

1. `cli.main()` parses args, loads `Config`, selects a `ModelConfig`, starts MCP
   (if configured), and builds an `Agent` via `agent.create_agent()`.
2. `Repl` (interactive) or `_run_print_mode` (one-shot) drives the agent.
3. `Agent.run(user_message)` appends to the `Session` and runs `_loop()`:
   call the model (`LLMClient.complete`) → if the response has `tool_calls`,
   execute each tool (through the permission gate) and append results → repeat
   until the model returns text with no tool calls, or `max_turns` is hit.
4. Tools implement `Tool.run(args, ctx) -> ToolResult`. `ToolContext` carries the
   cwd, the `PermissionManager`, the `Config`, and a `spawn_subagent` callable.
5. The `task` tool calls `spawn_subagent`, which builds a fresh `Agent` with a
   separate `Session` and a registry **without** the task tool (no infinite
   recursion), runs it non-interactively, and rolls usage back up to the parent.
6. **Skills** (`skills.py`): `discover_skills(cwd)` scans three locations —
   `coded/builtin_skills/` (bundled roles), `~/.config/coded/skills/`, and
   `./.coded/skills/` — for `<name>/SKILL.md`; a later location overrides an
   earlier one by name (so a project `planner` shadows the built-in). `create_agent`
   injects each skill's name+description into the system prompt (level 1) and
   registers the `skill` tool, which loads a skill's full body on demand (level 2).
   Bundled files are reached with the normal read/bash tools (level 3). Sub-agents
   run without skills. Frontmatter is parsed by a tiny built-in YAML subset (no
   pyyaml dependency) — scalars, inline `[a, b]`, and block lists only. Built-in
   role skills (`builtin_skills/`) ship via `[tool.setuptools.package-data]`.
7. **Review pipeline** (`review.py`): `run_review(agent, target)` runs the
   `reviewer`, `qa`, and `security` role skills in sequence, each via
   `Agent.run_subagent` (isolated context), and aggregates their reports. Exposed
   two ways: the REPL `/review [target]` command, and the non-interactive
   `coded review` subcommand for CI, which adds `synthesize_verdict()` (one
   model call → APPROVE/NEEDS_CHANGES) and maps NEEDS_CHANGES to exit code 1
   (`--fail-on`, `--yolo`, `--output`, `--no-verdict`).

## Key conventions

- **Adding a tool**: subclass `Tool` in `coded/tools/`, set `name`,
  `description`, `parameters` (JSON Schema), and implement `run()`. Register it
  in `tools/__init__.build_registry()`. Set `requires_permission = True` for
  anything that mutates the filesystem or runs commands, and override
  `permission_detail()` to describe the action shown in the approval prompt.
- **Tools must never raise into the loop**: return `ToolResult.error(...)` for
  expected failures. `Agent._run_tool` also catches unexpected exceptions and
  feeds them back to the model as text, so the agent can recover.
- **Per-call permission** (git/github): a tool that is safe for some inputs and
  risky for others sets `requires_permission = False` and calls
  `ctx.permissions.request(...)` itself for the risky branch. Static-risk tools
  use the class flag instead.
- **Checkpoints**: mutating file tools call `ctx.checkpoints.record(path)` before
  writing so `/undo` works. `bash` changes are intentionally not tracked. New
  mutating file tools must record.
- **Network/optional-key tools** (github/web/semantic_search) must degrade to a
  clear `ToolResult.error` when the key/backend/index is missing — never crash.
  Their heavy imports are done inside `run()` to keep startup light.
- **Offline tests**: `conftest.py` provides `FakeServer` (chat), `EmbeddingsServer`
  (deterministic embeddings), and `RouteServer` (arbitrary HTTP for web/github).
  Keep new network features testable against these, not the real internet.
- **Return text, not exceptions, to the model.** Error strings should tell the
  model what to do differently.
- **Adding a skill** (docs/instruction pack, not code): create
  `./.coded/skills/<name>/SKILL.md` with `name` + `description` frontmatter. The
  `description` drives auto-invocation — say what it does and *when* to use it.
  See `examples/skills/`. No code changes needed. A skill shipped with the tool
  goes in `coded/builtin_skills/<name>/SKILL.md` (e.g. the planner/reviewer/qa/
  security roles) and must be covered by the `package-data` glob in pyproject.
- **Adding a provider**: add an entry to `PROVIDERS` in `config.py` (base URL +
  API-key env var). Models reference it via `"provider": "<name>"`. Add local /
  keyless providers to `LOCAL_PROVIDERS` too. A model may instead be defined by
  just `base_url` + `model` (custom endpoint); `resolved_api_key()` falls back to
  a placeholder key so keyless endpoints work without config.
- **Model config fields** live on `ModelConfig`: `context_window`, `max_tokens`,
  `temperature`, `input_cost`/`output_cost` (per Mtok, used by `/cost`),
  `supports_tools`, `supports_vision`, `extra_headers`, and TLS (`verify_ssl`,
  `ca_bundle`). `config.example.json` is the catalogue and is validated by a
  test — keep it parseable and every model resolvable.
- **TLS**: both the LLM and embedding clients are built by `llm.build_openai_client`,
  which passes an `httpx.Client(verify=model.ssl_verify())` to the OpenAI SDK.
  `ssl_verify()` resolves `ca_bundle` → `$CODED_CA_BUNDLE` → `verify_ssl`. The
  `--ca-bundle`/`--insecure` CLI flags override the selected model; disabling
  verification warns. Route new endpoint clients through this helper.
- **UI**: use the helpers in `coded/ui.py` and its single shared `console`.
  Displayed tool output is truncated for readability; the **model** always
  receives the full tool result (truncation is display-only). Colors come from
  the palette constants (`ACCENT`, `MUTED`, …); `ui.thinking()` is the spinner
  used by the streaming path. The REPL (`repl.py`) adds a prompt_toolkit
  `bottom_toolbar` status bar, `AutoSuggestFromHistory`, and a shared `_PT_STYLE`.
- **Streaming**: `LLMClient._stream` accumulates text and tool-call deltas.
  `stream_options={"include_usage": true}` is requested but retried without it
  if the endpoint rejects it — keep this graceful-degradation habit for
  endpoints that implement only part of the OpenAI API.
- **Config is layered**: presets → user file → project file → CLI flags. Don't
  hard-code paths or keys; go through `config.py`.
- Follow the style already in the code: type hints, `from __future__ import
  annotations`, dataclasses for state, concise docstrings, minimal inline
  comments (only where they add real value).

## Development workflow

### Setup

```bash
pip install -e '.[dev]'        # editable install + pytest
pip install -e '.[mcp]'        # add MCP support
```

### Run

```bash
coded                          # REPL (needs a model configured or OPENAI_API_KEY)
coded -p "..."                 # one-shot
python -m coded                # equivalent entry point
```

To try it without real API keys, point `--base-url` at a local server, or see
the fake server in `tests/conftest.py`.

### Test

```bash
pytest                         # full suite
pytest tests/test_tools.py -q  # just the unit tests
```

Tests are **offline**: `tests/conftest.py` starts an in-process fake
OpenAI-compatible server, so the end-to-end tests exercise the real `openai`
SDK and the full agent loop without network access. When adding features,
prefer extending this fake-server approach over mocking internals.

### Lint / format

_No linter/formatter is configured yet._ Match the surrounding style. If you add
one (e.g. `ruff`, `black`), record the commands here.

## Gotchas

- **argparse + free-form prompt**: the top-level parser has a `nargs="*"`
  positional `prompt`, which conflicts with argparse subparsers. Subcommands
  (`config`, `models`) are therefore dispatched manually by the first token in
  `cli.main()` — keep new subcommands on that path, not on `add_subparsers`.
- **MCP runs on a background thread** with its own asyncio loop
  (`mcp_client.MCPManager`); calls into it are made with
  `run_coroutine_threadsafe`. Always `stop()` it (the CLI does so in a
  `finally`).
- **Permission default is deny** when non-interactive and not `--yolo`. Don't
  change this to "allow" — it's the safe default that keeps `--print` mode from
  running shell commands unattended. **Deny rules always win**, even over
  `--yolo`; per-tool `permission_target()` supplies the string matched by rules.
- **Compaction preserves tool pairing**: `compaction.py` only cuts the history
  at a `user`-role boundary, so an assistant `tool_calls` message is never split
  from its `tool` results (the API rejects that). Keep that invariant.
- **JSON print mode** (`--output-format json`) must keep **stdout clean**:
  `ui.set_quiet(True)` routes all log helpers to stderr, and the agent is forced
  non-verbose/non-streaming. Only the final JSON goes to stdout.

## Git & branching

- Development branch for AI-assisted changes in this session:
  `claude/claude-md-docs-n4n6zc`.
- Clear, descriptive commit messages. Do not open a pull request unless asked.
- Never commit real API keys. Project-level config lives in `./.coded/`, which
  is gitignored.

## Keeping this file accurate

Update this file whenever the structure, tooling, tool suite, config schema, or
conventions change. Verify commands and paths against the actual repository
before documenting them.
