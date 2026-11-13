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
├── permissions.py      # PermissionManager: approve/deny/always gate, accept-edits mode
├── trust.py            # project trust: which .coded/config.json keys need it, fingerprint store
├── choice.py           # Chooser: arrow-key selector for permission prompts + ask_user (review page)
├── notify.py           # Notifier: bell/desktop notification when a long turn ends or asks
├── tasks.py            # TaskManager: running sub-agents (progress, background, stop)
├── btw.py              # /btw: side question over a copy of the conversation (no tools)
├── prompts.py          # assembles main/sub-agent prompts + AGENTS.md/CODED.md/CLAUDE.md hierarchy
├── prompt_files.py     # prompt-file loading/overrides/profiles + `coded prompts` CLI
├── limits.py           # per-model limits scaled by context_window; result/step caps
├── repo_overview.py    # repository overview for large-context models
├── repomap.py          # ranked symbol map (ast + regex, PageRank over references)
├── depgraph.py         # import graph (Python ast, JS/TS regex): dependents, impact, cycles
├── memory.py           # project memory store (./.coded/memory.json)
├── speed.py            # `coded models --speed`: first token, total, tok/s per model
├── verify.py           # post-turn gates: project checks + a second model
├── routing.py          # difficulty classifier + model pools (round-robin, health)
├── builtin_prompts/    # system, coder, tools, security, skills, planner, reviewer, debugger (.md);
│                       # compact/ and full/ hold per-profile variants
├── repl.py             # interactive REPL and slash commands
├── tui.py              # full-screen Textual app (coded --tui), optional
├── sidebar.py          # the TUI's right panel: session, changed files, queue, sub-agents, shells
├── ui.py               # rich-based rendering helpers (single Console)
├── skills.py           # skill discovery, frontmatter parsing, prompt section
├── review.py           # /review pipeline: reviewer→qa→security sub-agents
├── builtin_skills/     # bundled roles: planner, reviewer, debugger (bodies from prompt files), qa, security
├── compaction.py       # context auto-compaction (summarize old messages)
├── sessions_store.py   # persist/restore sessions (--continue/--resume)
├── checkpoints.py      # CheckpointManager: per-turn file snapshots for /undo and /rewind
├── transcript.py       # /context breakdown and /export Markdown
├── ignore.py           # .codedignore (gitignore syntax): rules_for(cwd), cached by mtime
├── diagnostics.py      # per-file checks after an edit (config "diagnostics"), off by default
├── styles.py           # output styles (/style): builtin_styles/*.md < user < project
├── builtin_styles/     # explanatory, concise, learning (.md, package-data)
├── statusline.py       # user status-line command (JSON on stdin, cached, background)
├── doctor.py           # `coded doctor`: requirements, config, keys, tools, --network probes
├── update_check.py     # daily background `git ls-remote` for git checkouts; notice at start
├── embeddings.py       # EmbeddingClient + embedding-model resolution
├── index.py            # CodeIndex: chunk/embed/store + cosine search
├── websearch.py        # web-search backends + HTML→text fetch
├── netguard.py         # which URLs are internal (loopback/LAN/metadata/file) for web_fetch/browser
├── images.py           # image → base64 data URL for vision models; clipboard image grab
├── mentions.py         # `@path` mentions: expansion (Agent.run) + the REPL path completer
├── documents.py        # PDF/DOCX/XLSX/PPTX → text sections for the read tool
├── mcp_client.py       # optional MCP stdio client (background asyncio loop)
├── lsp.py              # LSP client: one language server per language, shared per process
├── hooks.py            # HookRunner: PreToolUse/PostToolUse/UserPromptSubmit/SessionStart/Stop
├── sandbox.py          # bwrap / sandbox-exec wrapping for bash (no Windows backend)
├── shells.py           # shell resolution (Git Bash/PowerShell/cmd), tree kill, UTF-8, rmtree
├── textio.py           # read/write text files preserving their line endings
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
├── agent_import.py     # `coded import`: other agents' MCP servers + Claude Code commands
└── tools/
    ├── __init__.py     # build_registry(): assembles the tool suite
    ├── base.py         # Tool, ToolResult, ToolContext, ToolRegistry
    ├── files.py        # read, write, edit, ls, move, delete
    ├── patch.py        # multi_edit, apply_patch (Codex patch format)
    ├── notebook.py     # notebook_edit (replace/insert/delete a cell; read renders .ipynb)
    ├── browser.py      # browser (Playwright; opt-in "browser"): render, steps, console, screenshot
    ├── todo.py         # todo_write (TodoStore), enter_plan_mode, exit_plan_mode
    ├── interact.py     # ask_user (PermissionManager.question), view_image (vision)
    ├── lsp.py          # lsp (opt-in "lsp"): definition/references/hover/symbols/diagnostics
    ├── external_agent.py  # external_agent (delegate to an outside CLI agent)
    ├── search.py       # glob, grep
    ├── repo_map.py     # repo_map (ranked outline of the codebase)
    ├── memory.py       # memory (remember/forget/list, gated per call)
    ├── semantic_search.py  # semantic_search (uses the embedded index)
    ├── shell.py        # bash (sandbox/background), bash_output, kill_shell
    ├── git.py          # git (read-only free, mutations gated)
    ├── github.py       # github (PR list/get/create, comment) via GITHUB_TOKEN
    ├── web.py          # web_search, web_fetch
    ├── task.py         # task (sub-agent delegation, run_in_background), task_output, task_stop
    ├── skill.py        # skill (loads a SKILL.md body into context)
    └── mcp_tool.py     # wraps an MCP server tool; list_mcp_resources, read_mcp_resource
tests/
├── conftest.py         # FakeServer + EmbeddingsServer + RouteServer (all offline)
├── test_agent_e2e.py   # full agent loop (tool call, streaming, permission)
├── test_tui.py         # Textual TUI via Pilot (turn, permission modal, clear)
├── test_sidebar.py     # the side panel: file counts, sections, width, Ctrl+B
├── test_review.py      # /review pipeline + coded review verdict/exit code
├── test_tasks.py       # sub-agent tasks: activity, background, Ctrl+B, /tasks, Ctrl+O
├── test_queue_edit.py  # ↑ edits the newest queued message (REPL reader, dock, TUI)
├── test_btw.py         # /btw: session untouched, answered mid-turn, REPL + TUI
├── test_choice.py      # selector keys/lines, review page, permission feedback, REPL dock, TUI
├── test_trust.py       # project trust gate: what applies untrusted, remembered fingerprint, CLI
├── test_session_autosave.py  # per-step autosave, atomic save, orphan tool calls, todos
├── test_memory_bounds.py  # bounded output/file reads, timeout kills the process group
├── test_compaction.py  # context compaction trigger + tail preservation
├── test_permissions_sessions.py  # allow/deny rules, diff preview, sessions, json
├── test_checkpoints.py # checkpoint/undo + move/delete
├── test_git_tools.py   # git tool + github tool (fake API)
├── test_semantic_and_web.py  # index/search, web_fetch/search, image encoding
├── test_skills.py      # skill discovery, frontmatter, skill tool
├── test_agents_command.py  # `coded agents`: states, install hints per platform, --check
├── test_skill_sources.py  # real-world collections: links, nesting, YAML, prompt budget
├── test_tools.py       # tool + config unit tests
├── test_routing.py     # difficulty routing, pools, endpoint reuse, per-model cost
├── test_repomap.py     # symbol extraction, ranking, budgets, the repo_map tool
├── test_depgraph.py    # import resolution, impact + tests to run, cycles, repo_map operations
├── test_memory.py      # memory store, the permission gate, prompt injection
├── test_documents.py   # reading PDF/Word/Excel/PowerPoint, refusals, no-extra path
├── test_speed.py       # lazy SDK import, disabled_tools, compact tool schema, --speed
├── test_verify.py      # the verification loop: checks, second opinion, rounds
├── test_model_chain.py # failover cooldown, sizing for a smaller fallback, stream retry,
│                       # per-model timeout, unknown-alias warnings
├── test_big_models.py  # scaled limits, caps, overflow retry, max_turns, profiles, repo overview
├── test_windows.py     # shell resolution, line endings, UTF-8, shims (runs everywhere)
├── test_prompts.py     # prompt files: placement, override order, empty=remove, CLI
├── test_agent_import.py  # `coded import`: each agent's format, skips, secrets, ${VAR}, commands
├── test_new_tools.py  # ask_user, view_image, enter_plan_mode, MCP resources
├── test_lsp.py        # the lsp tool against tests/fake_lsp.py (a scripted server)
├── test_plugins.py     # sources, zip safety, skill/plugin install/update/remove,
│                       # Claude-format plugins, marketplaces, CLI
└── test_modern_features.py  # todo/plan, hooks, sandbox, AGENTS.md, patch, parallel,
                        # retry/fallback/roles, reasoning, shells, commands, tracing,
                        # worktrees, injection guard, external agents, ACP, MCP server
examples/skills/        # example SKILL.md packs + install guide
examples/plugins/       # example plugin (code-quality) + plugin layout guide
examples/github-actions/coded-review.yml  # PR review workflow (copy to .github/workflows)
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
9a. **Which model answers a request** (`Agent._select_turn_model`, once per
   request, never per step): a forced model (`force_model`, a command's
   `model:`) > plan mode's `plan` role > difficulty routing (`routing.py`) >
   the main model. `switch_model()` pins the user's choice and turns routing
   off (`set_routing(True)` restores). `_turn_model` is the non-plan choice, so
   `exit_plan_mode` mid-turn returns to it. Routing refuses a target that lacks
   tools/vision or whose window the history no longer fits — never summarize to
   save money. Pools resolve only at named points (`Config.resolve_target`,
   `pick_from_pool`, `model_for_role`), never inside `get_model` on a hot path;
   `Agent._pool` makes the siblings fallbacks. Endpoints are process-wide
   (`llm.endpoint_for`), so health and learned-unsupported-params survive a
   switch. Cost accrues per answering model (`Session.record_usage(usage, model)`).

9. **Models per role**: `Config.model_for_role()` picks the model for
   `subagent`, `compact`, `review`, `verdict`, `plan` (`Agent.set_mode` swaps to
   the `plan` model via `use_model()` and back to `_main_model` on exit;
   `switch_model()` is the user's choice and sets `_main_model`); `Config.fallbacks_for()` resolves
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
- **Asking the user** (`ask_user`, 1-4 questions with headers, option
  descriptions, `multi_select`): `PermissionManager.questions(list[choice.Question])`
  goes to the front-end's `set_questions` callback (one answer string per
  question; `[]` = cancelled; None = nobody can answer: `--print`, ACP, which
  tells the model to state an assumption and go on). A front-end that only set
  the older `set_question(text, options)` is asked each question in turn.
  Main agent only, like `enter_plan_mode`, which refuses when
  `permissions.interactive` is false: nobody could approve the plan, so a
  `--print`/CI run would stay read-only.
- **Choice prompts** (`choice.py`): permission prompts ("Do you want to
  proceed?" → Yes / Yes, don't ask again / No, and tell coded what to do
  differently) and `ask_user` are one pure state machine, `Chooser`, fed key
  names (`key()`) or typed lines (`line()`) and drawn by `render()` — the same
  panel in the REPL and the TUI. Several questions get tabs and a "Review your
  answers → Submit" page. REPL: when the type-ahead reader can hand over keys
  (`TypeAhead.supports_keys`/`begin_keys`/`next_key`, the reader's
  `keys_mode`) and a `TurnDock` is up, the prompt replaces the dock's spinner
  and input box (`TurnDock.set_prompt`); otherwise it is printed with numbers
  and answered by a line (a follow-up typed at a permission prompt is
  requeued, never an answer). TUI: `PermissionModal`/`QuestionModal`
  (`ChoiceModal`). A "no" with text reaches the prompt callback as
  `"no: <text>"`; `request()` keeps it per thread and `Agent._run_tool` puts it
  in the denial (`take_feedback`).
- **`view_image`** queues a path through `ctx.extra["attach_image"]`; after the
  step's tool results the agent adds one user message with the images (a
  `tool` message cannot carry one). `Config.disabled_tools_for` drops it for a
  model without `supports_vision`, so it is neither offered nor runnable.
  That message is not a turn boundary: code that looks for "the last user
  message" must skip it (`agent._is_tool_images`).
  MCP `list_mcp_resources`/`read_mcp_resource` are built only when a server
  reported resources at start (`MCPManager.resource_servers`).
- **Code intelligence** (`lsp.py`, the `lsp` tool, opt-in `config.lsp`, like
  `browser`, so the default request is unchanged): plain JSON-RPC over the
  server's stdio, no dependency. `get_manager(root, settings)` is process-wide
  (sub-agents get the tool and share servers); `stop_all` runs at exit. Files
  are re-sent when their mtime or size changes (`LSPClient.sync`), so answers
  match disk. Source lines of `.codedignore`d files are never printed. Server requests (`workspace/configuration`, …) are always answered or
  some servers wait forever. Positions are named by `line` + `symbol`; LSP
  columns are UTF-16 (`utf16_col`/`py_col`). A missing server or unknown
  extension is a `ToolResult.error` pointing at grep/repo_map. Tests run
  against `tests/fake_lsp.py`, never a real server (one optional test skips).
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
- **`coded agents`** (`external_agents.agent_statuses`/`install_hint`, `AGENT_INFO`):
  status of every preset/configured outside agent and the install command for
  missing presets. Install commands are **printed, never run** (same reason as
  "no install tool"). `which`/`platform` are injectable, so tests don't depend
  on what is installed; keep `AGENT_INFO` keys equal to `PRESETS` (tested), and
  check new install commands against the real registry before adding them.
- **Skill sources** (`sources.py`, `plugins.find_skills`): a fetched source has
  every symlink deleted unread (`strip_symlinks`); `Fetched.refuse_links_in`
  then refuses only an install whose own files held one (a skill dir, or a
  whole plugin/marketplace root). Skills are searched at the root,
  `skills/*/`, `*/`, then up to `_MAX_DEPTH` deep without entering a skill's
  own folders; duplicate names are an error. skills.sh page links and GitHub
  `tree/` links are parsed into GitHub sources. The frontmatter parser is
  indentation-based (block lists, one-level maps, `>`/`|` and plain multi-line
  scalars) — still no pyyaml. `skills_prompt_section(budget=limits.skills_chars)`
  shortens a big list; under budget it must be byte-identical to unbudgeted.
- **No install tool for the model.** Installing skills/plugins grants code
  execution (hooks run unprompted), so it stays a user-run CLI action. Never
  write plugin contributions into the user's config.json; they're merged at load
  time so `disable`/`remove` are clean. The install summary must list every hook
  and MCP command verbatim; non-TTY installs require `--yes`.
- **Tools must never raise into the loop**: return `ToolResult.error(...)` for
  expected failures. `Agent._run_tool` also catches unexpected exceptions and
  feeds them back to the model as text, so the agent can recover.
- **Modes** (`Agent.permission_mode`/`cycle_mode`, Shift+Tab): default →
  accept-edits (`PermissionManager.accept_edits`: `EDIT_KEYS` run unasked; deny
  rules, plan mode and the strict injection guard still apply) → plan.
  Mid-turn Shift+Tab (`typeahead.SHIFT_TAB`, `ESC [Z`; TUI while busy) calls
  `Agent.toggle_accept_edits`: accept-edits on/off only, never plan (that swaps
  the model, so it changes between turns). `ui.TurnDock(get_mode=…)` shows it.
  `Config.max_cost` is checked by `Agent.over_budget()` at the top of every
  loop step (main agent only), so a turn stops cleanly with a note.
- **Per-call permission** (git/github): a tool that is safe for some inputs and
  risky for others sets `requires_permission = False` and calls
  `ctx.permissions.request(...)` itself for the risky branch. Static-risk tools
  use the class flag instead.
- **Checkpoints**: mutating file tools call `ctx.checkpoints.record(path)` before
  writing so `/undo` works. `/rewind` uses `Agent._turn_marks` (one per
  `Agent.run` of the main agent: the user message **by identity**, the typed
  text and the checkpoint group id); `turn_history()` drops marks whose message
  compaction or `/clear` removed, and `CheckpointManager.undo_since(id)` undoes
  that group and every later one, newest first.
- **Paths outside the project ask first** (`Agent._outside_paths`,
  `_SCOPED_TOOLS`): a path that resolves (symlinks included) outside the cwd,
  `add_dirs` and the skill folders needs a yes — read-only tools get their own
  prompt ("Read file outside the project"), mutating tools reuse theirs with
  `request(always_ask=True)`, so accept-edits mode and sandbox auto-allow do
  not skip it. `--yolo`, "always" and allow rules (`read(/path/*)`) answer it.
- **Internal addresses ask first** (`netguard.py`, `tools.web.url_permission`):
  `web_fetch` and `browser` ask (once per host, `always_ask`) before a local
  file, localhost, a private/link-local/metadata address, and re-check every
  redirect (`fetch_url(allow=…)`) or browser request (`page.route`); a
  `file://` inside the project passes. Non-interactive runs refuse them.
- **`.codedignore`** is enforced in `Agent._run_tool` for path tools
  (`_PATH_TOOLS`, plus apply_patch's file headers via `_tool_paths`), in
  `search._walk` (grep/glob), `ls`, and `@mentions`/their completion. bash is
  not covered (documented). **Diagnostics** run after `_CHECKED_TOOLS` succeed,
  only when `config.diagnostics` is set, and are appended to the tool result.
  `add_dirs` reach the Environment section only when non-empty (the prompt is
  otherwise byte-identical), the sandbox's writable list and mentions.
- **Opt-in extras must not change the default request**: the `browser` tool is
  registered only with `config.browser`, an output style only adds a prompt
  section when set, and `statusline`/`update_check` never block (background
  threads; tests set `CODED_NO_UPDATE_CHECK=1` via an autouse fixture).
- **Usage** carries `cached_tokens` (`llm.usage_from` reads OpenAI's
  `prompt_tokens_details.cached_tokens` and DeepSeek's `prompt_cache_hit_tokens`);
  `session.usage_cost` prices them at `cached_input_cost` when a model sets it. `bash` changes are intentionally not tracked. New
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
  logo (`ui.LOGO`: a block-letter "coded" wordmark shaded light → deep purple by
  `logo_text`). Never hard-code colors or
  reuse another product's logo/branding. Tool calls display as `● Edit(app.py)`
  with results under `⎿` (`ui.tool_call_text` / `tool_result_text` / `diff_text`
  / `welcome`); the REPL and the TUI both render through these so they match.
  A new tool gets a readable title by adding it to `ui._TOOL_TITLES`.
- **Transcript spacing** (REPL): every block (tool call, assistant text,
  line-mode prompt) starts with `ui.gap()`, which prints a blank line only if
  the last row was not blank — `ui.console` is a `TranscriptConsole` that
  tracks this for every `print` (a Live's `Control` redraw does not count), so
  there is exactly one empty line between blocks. Never start a block with a
  bare `console.print()`. Assistant text gets a `● ` with a hanging indent
  (`ui.answer_block`) only when the REPL turns it on (`set_answer_bullets`), so
  `-p` output stays plain.
- **UI**: use the helpers in `coded/ui.py` and its single shared `console`.
  Displayed tool output is truncated for readability; the **model** always
  receives the full tool result (truncation is display-only). Colors come from
  the palette constants (`ACCENT`, `MUTED`, …); `ui.thinking()` is the spinner
  used by the streaming path. The REPL (`repl.py`) adds a prompt_toolkit
  `bottom_toolbar` status bar, `AutoSuggestFromHistory` (Tab or → accepts it
  when no completion menu is open), and a shared `_PT_STYLE`.
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
  The right-hand panel is drawn by `sidebar.render` (no Textual import, so it is
  testable alone) from agent state; changed-file counts come from the current
  checkpoint group (`sidebar.file_changes`) and read disk, so they are
  recomputed only after a tool result, a turn or `/undo` — never per token.
- **Streaming**: `LLMClient._consume_stream` accumulates text, reasoning
  (`reasoning_content`/`reasoning`) and tool-call deltas. Optional params
  (`stream_options`, `reasoning_effort`, …) that an endpoint rejects with a 400
  are dropped and remembered per endpoint (`_OPTIONAL_PARAMS`). Keep this
  graceful-degradation habit for endpoints that implement only part of the
  OpenAI API. SDK retries are off (`max_retries=0`); `LLMClient._create` owns
  retry/backoff, and `complete()` owns fallback. A stream that fails is retried
  only if no text/reasoning was emitted yet (otherwise it would display twice).
- **Failures are explained, not dumped**: every error that ends a turn goes
  through `llm.explain_failure` (REPL, TUI and `--print`), which names the model
  and base_url and gives the next step. A 5xx gets one last attempt with the
  optional params (`_OPTIONAL_PARAMS`) removed, since some gateways answer an
  unimplemented param with a 500; `tools` is never dropped automatically.
  `coded models --check` probes a failing endpoint in stages
  (`llm.probe_stages`/`probe_hint`) to say which part of the request it refuses.
- **Startup and per-request size**: `openai`/`httpx` are lazy in `llm.py`
  (`_LazyModule`, warmed by `llm.prewarm()` on a thread from `cli.main`), so
  importing `coded.cli`/`coded.agent` must not import them — a test checks.
  Don't add top-level imports of heavy libraries; import inside the function.
  Tool definitions go out with every request: `Agent._tool_schema()` applies
  `Config.disabled_tools_for(model)` (fnmatch patterns, global + per model)
  and, for the compact prompt profile, `Tool.brief` plus argument descriptions
  cut to a sentence (`base._compact_parameters`). The standard profile's schema
  must stay byte-identical. A disabled tool is also refused in `_run_tool`.
  `coded models --speed` (`speed.py`) times the real turn shape.
- **Documents** (`documents.py`, through the `read` tool): PDF/DOCX/XLSX/PPTX
  become sections (pages, slides, sheets) of text lines, then go through the
  same offset/limit/budget code as text. The parsers are the `docs` extra and
  are imported lazily; a missing one is a `DocumentError` naming the install,
  and a binary file must **never** be decoded as text (a PDF is recognised by
  magic, and text with too many replacement characters is refused). Parsers
  raise anything, so `documents.load` converts every exception.
- **Repository map** (`repomap.py`, the `repo_map` tool): `ast` for Python plus
  conservative regexes elsewhere — an outline, not a parse tree, and never a
  new dependency. Ranking is PageRank over a file graph built from "names a
  symbol another file defines"; methods and names with more than `_MAX_DEFINERS`
  definers are excluded, or ubiquitous names decide the ranking. Everything is
  bounded (files, bytes, seconds, output) and nothing raises. It is a **tool**,
  not a prompt section: the system prompt stays names-only, and a 128k model
  must behave exactly as before.
- **Dependency map** (`depgraph.py`, `repo_map` with `operation`): `imports`,
  `dependents`, `impact` (reverse-transitive with depth, plus the test files
  among them: `affected_tests`) and `cycles`. Python imports via `ast`
  (absolute, relative, `src/`/`lib/` layouts, `from pkg import submodule`);
  imports inside a function are recorded in `DepGraph.lazy` — they count for
  impact but not for import-time cycles. JS/TS: relative specifiers only,
  resolved like a bundler (extensions, `index.*`, `.js`→`.ts`). Bounded,
  honours `.codedignore`, never raises. Same tool, not a new one: an omitted
  `operation` is the old outline.
- **Project memory** (`memory.py`, the `memory` tool, `/memory`): short notes in
  `./.coded/memory.json`, injected by `build_system_prompt` as labelled data
  within `limits.memory_chars`. The model writes only the **project** store and
  only through `ctx.permissions.request` (per-call, like git — never
  `requires_permission`, which would also drag the turn through the verify
  gate), and not at all while `permissions.tainted`. Every write re-reads first,
  so a concurrent session's note is not clobbered. Sub-agents get no memory
  tool. An empty store must leave the system prompt byte-identical.
- **Verification** (`verify.py`, `Agent._verify_turn`): after a turn that
  changed something, the project's `verify.command` and/or the `verify` role
  model gate the answer; a complaint goes back as a `[verify]` user message,
  bounded by `max_rounds`, then released with a visible note. The gate is "did
  a tool with `requires_permission` or `mutates` run", **not** the checkpoint
  group — `bash` edits are not checkpointed. Checkpoints only supply the diff.
  Never runs for sub-agents, plan mode or an unchanged turn; an unparseable
  verdict and a reviewer outage both release the turn. The second opinion needs
  `model_roles.verify` set explicitly: never auto-assign a critic.
- **Last-resort fallback** (`Config.auto_fallback`, on): when a model's own
  chain is exhausted, `Config.last_resort_models` offers the other configured
  models (same endpoint first, then the widest window) and `LLMClient._chain`
  drops the ones that cannot serve *this* request — no vision for an image, no
  tools for a tool turn, no window smaller than the conversation (never compact
  to survive someone else's outage). Declared fallbacks are never filtered.
  Each gets one attempt, a host that refuses a connection is skipped for its
  other models, and the error raised is the **primary's**, carrying
  `coded_also_tried`.
- **Model chain**: an endpoint that fails over is skipped for
  `fallback_cooldown` seconds (`_Endpoint.down_until`; cooling endpoints are
  tried last). `LLMClient.next_model()` is the model the next request goes to;
  `Agent._sync_sizing()` (each step, and on overflow) makes `self.limits` and
  compaction follow it via `_sized_model`, shrinking history when it is
  smaller. Use `_sized_model`, not `self.model`, for anything window-dependent
  in the loop. `Config.model_warnings()` reports unknown aliases.
- **Bounded memory**: never hold a command's output or a whole file of unknown
  size. `shells.run` spools output to temp files and keeps the head and tail
  past `max_bytes` (`MAX_CAPTURE_BYTES`); `grep` skips files over
  `GREP_MAX_FILE_BYTES`; `read` streams text over `READ_STREAM_BYTES`;
  background reads return at most `BG_READ_MAX_BYTES` of the newest output
  (`tests/test_memory_bounds.py`). Unbounded reads let a sub-agent grow coded
  until the OS SIGKILLed it.
- **Sessions survive a crash**: `Agent.on_step` (main agent only; the CLI sets
  it to autosave) runs after the user message, each tool step and the final
  answer, so a kill loses at most one step; the REPL also saves after Ctrl-C.
  `SessionStore.save` writes atomically (temp + `os.replace`). The task list
  rides along as `Session.todos`: the CLI's autosave copies `agent.todos.items`
  into it, and `--continue`/`--resume` put it back into `agent.todos`.
  `sessions_store.close_tool_calls` gives unanswered tool calls an
  `INTERRUPTED_RESULT`: applied on load and at the start of every
  `Agent.run`, since endpoints reject a history with an orphan tool call.
- **Cross-platform**: anything that runs a command goes through `shells.run` /
  `command_argv` (POSIX keeps the historical `shell=True` string path; Windows
  gets an argv list for the resolved shell) and `popen_kwargs`/`kill_tree` for
  process groups. Capture output with `**text_kwargs()`, never bare `text=True`
  (Windows would decode with the ANSI code page). Editing a file goes through
  `textio.read_text`/`write_text` so line endings survive; never `Path.write_text`
  for user files. Platform logic takes `which`/`exists`/`env` arguments so it is
  testable on any OS (`tests/test_windows.py`). The shell is settled once, by
  `load_config` → `shells.ensure_shell` (or `--shell`, which pins it), before
  plugin hook paths or the system prompt are built. Decoding a file is strict:
  edits refuse non-UTF-8 files (`textio.NOT_UTF8`) rather than rewriting them
  from a lossy read. Windows shells differ in more than quoting: PowerShell
  commands go through `-EncodedCommand` (and its CLIXML error stream is decoded
  back to text by `shells.clean_clixml`), cmd goes through subprocess's own
  `shell=True`, and `prompts._environment` tells the model which syntax to write
  (sub-agents included). An unknown `shell` name is a ConfigError, never a silent
  fallback. Arguments passed to a `.bat`/`.cmd` shim are re-parsed by cmd, so
  `external_agents.cmd_shim_risk` refuses prompts it could execute.
- **`coded import`** (`agent_import.py`): reads the MCP servers other agents
  configured (Claude Code, Claude Desktop, Cursor, VS Code, Gemini CLI, Codex
  TOML, Windsurf; paths in `candidate_files`, `home`/`platform`/`env`
  injectable), converts them with `convert`, prints every command verbatim and
  writes one config file's raw JSON through `config_wizard.read_file`/
  `write_file`, after a confirmation (non-TTY needs `--yes`). It never
  overwrites a name the file has, skips URL servers (stdio only), refuses
  literal `env` values for the project file and tightens the user file to 600
  when it adds one. `mcp_client.server_params` expands `${VAR}`/`${VAR:-d}`
  in command, args and env, the form other agents' configs use for secrets;
  an unset one is named in `MCPManager.errors` (`unset_refs`).
  It also copies Claude Code commands (`discover_commands`: `~/.claude/commands`
  → the user commands dir, `./.claude/commands` → `./.coded/commands`, or all
  into the project with `--project`) byte for byte, except a frontmatter
  `model:` that is not a coded model, which is dropped (coded refuses an unknown
  one). Built-in slash-command names and existing files are skipped.
- **Interactive config** (`config_wizard.py`, `coded config setup`, REPL
  `/setup`): edits **one config file's raw JSON**, never a merged `Config` —
  otherwise another layer's models and plugin contributions would be written
  into the user's file. Unknown keys survive; writes are atomic and confirmed;
  API keys default to an env-var name and a literal key only ever reaches the
  user file (mode 600), never the project one. All prompting goes through
  `Prompter`, so the flow is testable without a TTY.
- **A long turn must stay legible and interruptible**: task progress goes
  through `ui.todo_progress`/`todo_bar`/`todo_title`/`current_todo` (panel, REPL
  toolbar and TUI status line all use them), and in the REPL `ui.todo_dock` pins the
  unfinished list under the spinner and the streaming view (transient, so the
  final answer frame drops it), and input typed mid-turn is queued. During a
  REPL turn `ui.TurnDock` is the one rich Live (spinner, tasks, an input box
  showing `TypeAhead.typing`); the key reader's echo is off meanwhile, and
  `ui.thinking()`/`_call_streaming` draw into the dock (`ui.active_dock()`)
  instead of opening their own Live — rich allows only one. Input typed mid-turn is queued
  by `typeahead.TypeAhead` (REPL) or `CodedTUI._queued` and run as the next
  message. The type-ahead reader owns stdin during a turn, so a prompt that
  needs an answer asks it via `TypeAhead.ask()` instead of reading stdin itself.
  Polling (never a blocking read) is what lets the reader stop with the turn
  instead of stealing the next prompt's first line. On POSIX the reader
  (`typeahead._PosixKeys`) puts the terminal in non-canonical, no-echo mode for
  the turn (signals stay on) so a lone **Esc** is seen; escape sequences (arrow
  keys) are dropped, and `TypeAhead.stop()` must restore the terminal (`close()`).
  Esc stops the turn by sending this process SIGINT, so it behaves exactly as
  Ctrl-C.
- **Tasks** (`tasks.py`, `Agent.tasks`, `ctx.extra["tasks"]`): every sub-agent
  run is a `TaskEntry`; the sub-agent's `on_activity` (fired in
  `_show_tool_call`) records its steps. `task` with `run_in_background` runs
  `_work` on a thread and returns at once; with `TaskManager.can_detach` (set by
  the REPL for the turn when the type-ahead reader runs) a foreground task also
  runs on a worker so **Ctrl+B** (`entry.detach`) can return early, and Esc/
  Ctrl-C stops it. A background thread calls `permissions.set_background`, so
  `request` denies instead of prompting and `ask`/`question` return None
  (the same once a task is stopped: its thread may still be finishing a step).
  Usage is added under `session.USAGE_LOCK` (inside `Session.record_usage`).
  Finished reports reach the agent as one user message (`report_message`): at
  the top of each step (`_deliver_background`), before a final answer
  (`_background_pending`, which also waits when `wait_for_background` — true
  except in the REPL), or by the REPL waking an idle prompt (`_WAKE` via
  `tasks.on_finish`). The REPL shows tasks in `ui.TurnDock`
  (`ui.running_tasks`), the status bar and `/tasks`; the TUI in the side
  panel's "Sub-agents" section (`sidebar.render`, refreshed on `task_*`
  events and by `_tick` while one runs) and a `task_end` note in the
  transcript. ("Tasks" in the sidebar is the todo list.) Control keys mid-turn
  (`typeahead.CTRL_B/O/T`) go to `TypeAhead(keys=…)`; the POSIX reader clears
  IEXTEN so ^O/^T reach it. **Ctrl+O** flips `ui.set_expanded`: `ui.tool_result`
  then prints whole results and `show_last_cut` re-prints every result
  shortened this turn (`_cuts`, cleared by `reset_cuts` at each turn), each
  under its tool call. Folded output is 3 lines (`_MAX_RESULT_LINES`), blank runs squeezed and
  long lines cut to one terminal row; the TUI's `ToolResult` folds the same way
  and a click (or Ctrl+O, `action_toggle_output`, for all) expands it.
- **Queued messages** (`TypeAhead._queued`, a deque): **↑** mid-turn
  (`typeahead.UP`, from `ESC [A`/`ESC OA` or Windows `\xe0H`) runs
  `edit_last`, moving the newest one into the line being typed; Enter queues it
  again. Messages left from earlier turns are requeued into each new turn's
  reader so they stay visible (`ui.queued_list` in the dock) and editable. TUI:
  the `up` binding (`action_edit_queued`) does the same when the prompt has focus.
- **`/btw`** (`btw.py`): one `LLMClient.complete` on a fresh client
  (`agent._make_llm`) over `side_messages` — a copy of the session with an
  in-flight tool step dropped and orphans closed, plus `BTW_INSTRUCTION` and the
  question — with the agent's normal tool schema (valid history, same cached
  prefix); a reply that is only a tool call is asked again once with no tools. Never appended to the session; usage is
  recorded. Mid-turn it is taken out of the queue by `TypeAhead(intercept=…)`
  (REPL) or before the busy check (TUI) and answered on its own thread.
- **`@path` mentions** (`mentions.py`) are expanded in `Agent.run` (main agent
  only) so every front-end gets them: a mention starts the message or follows
  whitespace and must name something that exists (cwd or `add_dirs`), so emails
  and decorators are left alone. The original text is kept; the files follow in
  a `<mentioned-files>` block, images go to the image list for vision models.
- **Project trust** (`trust.py`, `config._merge_project`): a repository's
  `./.coded/config.json` is written by whoever wrote the repository. Only
  `trust.SAFE_KEYS` (output/model-choice settings), deny rules, a stricter
  `injection_guard` and models that just name a known provider apply at once;
  everything else (hooks, MCP servers, outside agents, endpoints/keys/headers,
  allow rules, auto-approve, sandbox, shell, statusline, verify, lsp, …) and
  project-scope plugins apply only to a trusted project. Trust is stored per
  realpath with a fingerprint of those settings (`~/.config/coded/trusted.json`,
  `$CODED_TRUST_STORE` in tests), so a changed hook asks again. The REPL/TUI
  start asks once (`cli._ask_project_trust`, every command verbatim); `-p`,
  `review`, `commit`, ACP and `mcp-serve` stay untrusted with a stderr note
  unless `--trust-project` / `CODED_TRUST_PROJECT=1`. A new config key is
  sensitive unless added to `SAFE_KEYS` on purpose. Permission rule lists add
  up across layers (a later file can never drop a deny rule).
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
pip install -e '.[docs]'       # read PDF/Word/Excel/PowerPoint
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
  (`config`, `models`, `agents`, `doctor`, `index`, `review`, `sessions`, `commit`, `import`, `skill`,
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

## Releasing

Bump the version in `pyproject.toml` and `coded/__init__.py` (a test keeps them
equal), add a `## <version>` section to `CHANGELOG.md`, merge to main, then push
a tag `v<version>` on main. `.github/workflows/release.yml` checks the tag
against the package version and creates the GitHub Release from that section.

## Keeping this file accurate

Update this file whenever the structure, tooling, tool suite, config schema, or
conventions change. Verify commands and paths against the actual repository
before documenting them.
