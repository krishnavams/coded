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
├── prompts.py          # assembles main/sub-agent prompts + AGENTS.md/CODED.md/CLAUDE.md hierarchy
├── prompt_files.py     # prompt-file loading/overrides/profiles + `coded prompts` CLI
├── limits.py           # per-model limits scaled by context_window; result/step caps
├── repo_overview.py    # repository overview for large-context models
├── builtin_prompts/    # system, coder, tools, security, skills, planner, reviewer, debugger (.md);
│                       # compact/ and full/ hold per-profile variants
├── repl.py             # interactive REPL and slash commands
├── tui.py              # full-screen Textual app (coded --tui), optional
├── ui.py               # rich-based rendering helpers (single Console)
├── skills.py           # skill discovery, frontmatter parsing, prompt section
├── review.py           # /review pipeline: reviewer→qa→security sub-agents
├── builtin_skills/     # bundled roles: planner, reviewer, debugger (bodies from prompt files), qa, security
├── compaction.py       # context auto-compaction (summarize old messages)
├── sessions_store.py   # persist/restore sessions (--continue/--resume)
├── checkpoints.py      # CheckpointManager: per-turn file snapshots for /undo
├── embeddings.py       # EmbeddingClient + embedding-model resolution
├── index.py            # CodeIndex: chunk/embed/store + cosine search
├── websearch.py        # web-search backends + HTML→text fetch
├── images.py           # image → base64 data URL for vision models
├── mcp_client.py       # optional MCP stdio client (background asyncio loop)
├── hooks.py            # HookRunner: PreToolUse/PostToolUse/UserPromptSubmit/SessionStart/Stop
├── sandbox.py          # bwrap / sandbox-exec wrapping for bash
├── background.py       # BackgroundShells: run_in_background processes
├── commands.py         # custom slash commands (.coded/commands/*.md)
├── telemetry.py        # Tracer: JSONL spans + optional OpenTelemetry
├── worktree.py         # git worktree isolation for task / external_agent
├── external_agents.py  # outside agent CLIs (claude, codex, gemini, aider, …)
├── acp.py              # `coded acp`: Agent Client Protocol server (stdio JSON-RPC)
├── mcp_server.py       # `coded mcp-serve`: coded as an MCP server
├── sources.py          # fetch dir / zip (safe extract) / git / GitHub shorthand into a temp dir
├── plugins.py          # skill + plugin install, registry (installed.json), marketplaces,
│                       # plugin contributions merged into skills/commands/config
├── manage_cli.py       # `coded skill …` and `coded plugin …` subcommands
└── tools/
    ├── __init__.py     # build_registry(): assembles the tool suite
    ├── base.py         # Tool, ToolResult, ToolContext, ToolRegistry
    ├── files.py        # read, write, edit, ls, move, delete
    ├── patch.py        # multi_edit, apply_patch (Codex patch format)
    ├── todo.py         # todo_write (TodoStore), exit_plan_mode
    ├── external_agent.py  # external_agent (delegate to an outside CLI agent)
    ├── search.py       # glob, grep
    ├── semantic_search.py  # semantic_search (uses the embedded index)
    ├── shell.py        # bash (sandbox/background), bash_output, kill_shell
    ├── git.py          # git (read-only free, mutations gated)
    ├── github.py       # github (PR list/get/create, comment) via GITHUB_TOKEN
    ├── web.py          # web_search, web_fetch
    ├── task.py         # task (sub-agent delegation)
    ├── skill.py        # skill (loads a SKILL.md body into context)
    └── mcp_tool.py     # wraps an MCP server tool as a coded Tool
tests/
├── conftest.py         # FakeServer + EmbeddingsServer + RouteServer (all offline)
├── test_agent_e2e.py   # full agent loop (tool call, streaming, permission)
├── test_tui.py         # Textual TUI via Pilot (turn, permission modal, clear)
├── test_review.py      # /review pipeline + coded review verdict/exit code
├── test_compaction.py  # context compaction trigger + tail preservation
├── test_permissions_sessions.py  # allow/deny rules, diff preview, sessions, json
├── test_checkpoints.py # checkpoint/undo + move/delete
├── test_git_tools.py   # git tool + github tool (fake API)
├── test_semantic_and_web.py  # index/search, web_fetch/search, image encoding
├── test_skills.py      # skill discovery, frontmatter, skill tool
├── test_tools.py       # tool + config unit tests
├── test_model_chain.py # failover cooldown, sizing for a smaller fallback, stream retry,
│                       # per-model timeout, unknown-alias warnings
├── test_big_models.py  # scaled limits, caps, overflow retry, max_turns, profiles, repo overview
├── test_prompts.py     # prompt files: placement, override order, empty=remove, CLI
├── test_plugins.py     # sources, zip safety, skill/plugin install/update/remove,
│                       # Claude-format plugins, marketplaces, CLI
└── test_modern_features.py  # todo/plan, hooks, sandbox, AGENTS.md, patch, parallel,
                        # retry/fallback/roles, reasoning, shells, commands, tracing,
                        # worktrees, injection guard, external agents, ACP, MCP server
examples/skills/        # example SKILL.md packs + install guide
examples/plugins/       # example plugin (code-quality) + plugin layout guide
examples/.coded-plugin/marketplace.json  # examples folder doubles as a marketplace
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

8. **Turn lifecycle** (`Agent.run`/`_loop`/`_run_tool`): `SessionStart` (first
   turn) and `UserPromptSubmit` hooks → plan-mode reminder appended → model call
   (`LLMClient`: retries, dropped unsupported params, `fallback` models) → tool
   calls, with consecutive parallel-safe calls run on a thread pool → per call:
   `PreToolUse` hook → plan-mode refusal → diff preview → permission gate
   (`auto_allowed` for sandboxed bash) → run → untrusted-output fencing/taint →
   nested `AGENTS.md` injection for file tools → `PostToolUse` hook. A final
   answer runs the `Stop` hook, which may push the agent to continue.
9. **Models per role**: `Config.model_for_role()` picks the model for
   `subagent`, `compact`, `review`, `verdict`; `Config.fallbacks_for()` resolves
   a model's `fallback` aliases for `LLMClient`.

10. **Skills & plugins install** (`plugins.py`): `coded skill|plugin …` fetch a
    source into a temp dir (`sources.Fetched`), validate it, show a summary and
    ask, then `install_tree` swaps it into `<home>/skills|plugins/<name>` and
    records the source in `<home>/installed.json` (`home` = `~/.config/coded`
    or `./.coded` with `--project`). Enabled plugins are merged at load time:
    `skill_search_dirs` (built-in < plugin < user < project), `command_dirs`
    (`/<plugin>:<cmd>`), and `load_config` → `apply_plugins_to_config` (hooks
    appended, MCP servers/outside agents added unless the user defines the name).

11. **Prompts are files** (`prompt_files.py`): `build_system_prompt` joins
    `system` + `coder` + `tools` + `security`, then an Environment section built
    in code, project instructions and extras. `build_subagent_prompt` uses a
    fixed sub-agent opener (tests rely on its first line) + `coder` + `tools` +
    `security`. A skill whose body is `<!-- coded:prompt NAME -->` loads prompt
    file NAME (planner/reviewer/debugger), so there is one source of truth.

12. **Per-model scaling** (`limits.py`): `limits_for(model)` scales the base
    (128k) limits by `context_window`, adds the per-result/per-step caps and the
    repo-overview budget, then applies the model's `limits` overrides. Each
    Agent (sub-agents too) computes its own and exposes them to tools as
    `ctx.extra["limits"]` (read with `get_limits(ctx)`), alongside
    `ctx.extra["model"]`. Prompt loading takes the model (`load_prompt(name,
    cwd, model)`): precedence built-in < built-in profile < user < project <
    model `prompts_dir`. `Agent.refresh_system_prompt()` rebuilds messages[0]
    (called by `create_agent` and `switch_model`).

## Key conventions

- **Adding a tool**: subclass `Tool` in `coded/tools/`, set `name`,
  `description`, `parameters` (JSON Schema), and implement `run()`. Register it
  in `tools/__init__.build_registry()`. Set `requires_permission = True` for
  anything that mutates the filesystem or runs commands, and override
  `permission_detail()` to describe the action shown in the approval prompt.
- **Tool flags**: `parallel_safe = True` only for read-only tools with no
  permission prompt (or override `is_parallel_safe(args, ctx)`, as `task` does
  for worktree isolation). `untrusted_output = True` for tools returning
  third-party content (web, GitHub, MCP) so it's fenced and taints the turn.
  `auto_allowed(args, ctx)` lets a call skip the prompt (sandboxed bash);
  deny rules, plan mode and the strict injection guard still apply.
- **Shared agent state reaches tools via `ToolContext.extra`**: `todos`,
  `shells`, `get_mode`/`set_mode`. Sub-agents share the parent's
  `PermissionManager`, tracer and shells, but not todos/skills/Stop hooks.
- **Prompts from worker threads**: `PermissionManager.set_prompt(fn,
  threadsafe=True)` (TUI, ACP) allows tools that may prompt to run in parallel;
  prompts are serialized by a lock either way. `ask()` is a raw yes/no question
  (used by `exit_plan_mode`).
- **The system and role prompts live in `coded/builtin_prompts/*.md`.** (Short
  operational prompts — the sub-agent opener, `PLAN_MODE_REMINDER`, review-phase
  framing, compaction/verdict/`/init` prompts — are still in code.) Never
  `str.format` prompt files (users' overrides may contain braces); put
  dynamic values in code-built sections. New prompt files must be added to
  `PROMPT_NAMES`, and stay covered by the `builtin_prompts/*.md` package-data
  glob. Watch the size: coded targets small local models too.
- **A 128k model must behave exactly as before.** New size-dependent behaviour
  goes through `limits_for`; `test_128k_limits_equal_the_historical_constants`
  pins the base values. Tools never hard-code output sizes: use `get_limits(ctx)`.
  Every prompt profile must keep the untrusted-content and secrets rules (tested).
- **No install tool for the model.** Installing skills/plugins grants code
  execution (hooks run unprompted), so it stays a user-run CLI action. Never
  write plugin contributions into the user's config.json; they're merged at load
  time so `disable`/`remove` are clean. The install summary must list every hook
  and MCP command verbatim; non-TTY installs require `--yes`.
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
- **Look**: purple theme (`ui.ACCENT`/`ACCENT_DEEP`/`ACCENT_SOFT`) and coded's own
  logo (`ui.LOGO`: code brackets around a diamond). Never hard-code colors or
  reuse another product's logo/branding. Tool calls display as `● Edit(app.py)`
  with results under `⎿` (`ui.tool_call_text` / `tool_result_text` / `diff_text`
  / `welcome`); the REPL and the TUI both render through these so they match.
  A new tool gets a readable title by adding it to `ui._TOOL_TITLES`.
- **UI**: use the helpers in `coded/ui.py` and its single shared `console`.
  Displayed tool output is truncated for readability; the **model** always
  receives the full tool result (truncation is display-only). Colors come from
  the palette constants (`ACCENT`, `MUTED`, …); `ui.thinking()` is the spinner
  used by the streaming path. The REPL (`repl.py`) adds a prompt_toolkit
  `bottom_toolbar` status bar, `AutoSuggestFromHistory`, and a shared `_PT_STYLE`.
- **Agent display is decoupled via events**: `Agent.on_event` (set by the TUI)
  receives structured events — `assistant_start/_delta/_end`, `reasoning_delta`,
  `tool_call`/`tool_result`/`diff` (with the tool-call `id`), `todos`, `status`.
  The TUI and the ACP server (`acp.py`) are both event consumers. When it's set, the agent emits instead of
  printing via `ui` (see `_emit`, `_show_tool_*`, `_call_with_events`). New
  display sites must route through these helpers, not call `ui` directly, so
  both front-ends stay in sync.
- **TUI** (`tui.py`, Textual, optional `[tui]` extra): the synchronous agent
  runs in a `@work(thread=True)` worker; events are marshalled to the UI thread
  with `call_from_thread`. The permission callback pushes a `PermissionModal`
  and blocks the worker on a `threading.Event` until the user answers. Guard the
  Textual import — the app must run fine without it installed.
- **Streaming**: `LLMClient._consume_stream` accumulates text, reasoning
  (`reasoning_content`/`reasoning`) and tool-call deltas. Optional params
  (`stream_options`, `reasoning_effort`, …) that an endpoint rejects with a 400
  are dropped and remembered per endpoint (`_OPTIONAL_PARAMS`). Keep this
  graceful-degradation habit for endpoints that implement only part of the
  OpenAI API. SDK retries are off (`max_retries=0`); `LLMClient._create` owns
  retry/backoff, and `complete()` owns fallback. A stream that fails is retried
  only if no text/reasoning was emitted yet (otherwise it would display twice).
- **Model chain**: an endpoint that fails over is skipped for
  `fallback_cooldown` seconds (`_Endpoint.down_until`; cooling endpoints are
  tried last). `LLMClient.next_model()` is the model the next request goes to;
  `Agent._sync_sizing()` (each step, and on overflow) makes `self.limits` and
  compaction follow it via `_sized_model`, shrinking history when it is
  smaller. Use `_sized_model`, not `self.model`, for anything window-dependent
  in the loop. `Config.model_warnings()` reports unknown aliases.
- **Config is layered**: presets → user file → project file → CLI flags. Don't
  hard-code paths or keys; go through `config.py`. Dict-valued settings
  (`hooks`, `sandbox`, `telemetry`, `external_agents`, `model_roles`) merge
  key-by-key across layers.
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
  (`config`, `models`, `index`, `review`, `sessions`, `commit`, `skill`,
  `plugin`, `prompts`, `acp`, `mcp-serve`) are therefore dispatched manually by the first token in
  `cli.main()` — keep new subcommands on that path, not on `add_subparsers`.
- **ACP / MCP server modes keep stdout clean** (`ui.set_quiet(True)`): stdout
  carries only protocol messages.
- **Sandbox `/tmp` is private** (bwrap `--tmpfs /tmp`): writes there vanish
  after the command. The working directory is bind-mounted writable, so tests
  using `tmp_path` as cwd still work.
- **Parallel batches display after they finish**: the parallel path calls
  `_run_tool(tc, display=False)` and then shows calls/results in order; keep
  new display code inside `_run_tool`'s `display` guard.
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

- Develop on the branch assigned to your session.
- Clear, descriptive commit messages. Do not open a pull request unless asked.
- Never commit real API keys. Project-level config lives in `./.coded/`, which
  is gitignored.

## Keeping this file accurate

Update this file whenever the structure, tooling, tool suite, config schema, or
conventions change. Verify commands and paths against the actual repository
before documenting them.
