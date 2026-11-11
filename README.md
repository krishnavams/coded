# coded

A **Claude Code-style terminal coding agent** that works with any
**OpenAI-compatible** model — OpenAI, Groq, DeepSeek, OpenRouter, Together,
Fireworks, xAI, Mistral, or local runtimes like Ollama, LM Studio and
llama.cpp. Register as many models as you like and switch between them at
runtime.

```
┌───────────────────────────────────────────────┐
│ coded — a coding agent for OpenAI-compatible … │
│                                                │
│ model: gpt-4o                                  │
│ cwd:   /home/you/project                       │
│                                                │
│ Type your request, or /help for commands.      │
└───────────────────────────────────────────────┘
› add a --verbose flag to the CLI and update the README
```

## Features

- **Agentic tool loop** — the model reads, writes, and edits files, searches the
  codebase, and runs shell commands until your request is done.
- **Full tool suite** — `read`, `write`, `edit`, `multi_edit`, `apply_patch`,
  `ls`, `move`, `delete`, `glob`, `grep`, `semantic_search`, `bash` (+
  `bash_output` / `kill_shell` for background jobs), `git`, `github`,
  `web_search`, `web_fetch`, `todo_write`, `exit_plan_mode`, `skill`, `task`
  (sub-agents) and `external_agent` (outside agents).
- **Task list & plan mode** — the agent tracks multi-step work with
  `todo_write`; `--plan` / `/plan` keeps it read-only until you approve a plan.
- **Hooks** — run your own commands on `PreToolUse`, `PostToolUse`,
  `UserPromptSubmit`, `SessionStart` and `Stop` (block actions, auto-format,
  keep the agent going until tests pass).
- **OS sandbox** — `--sandbox` runs `bash` under bubblewrap (Linux) or
  `sandbox-exec` (macOS): writes limited to the project, no network, no prompts.
- **Parallel tools** — independent reads/searches requested together run concurrently.
- **Many endpoints, models & keys** — per-model endpoint/key, automatic retry
  with backoff, **fallback** to another endpoint/model, **per-role models**
  (cheap sub-agents/compaction, strong reviews), and `coded models --check`.
- **Reasoning models** — `reasoning_effort`, provider `extra_body`, and
  streamed thinking (`--show-reasoning`).
- **Outside agents** — delegate to Claude Code, Codex, Gemini CLI, Aider,
  OpenCode, Cursor or Goose when they're installed, or any CLI you configure.
- **Worktree isolation** — sub-agents and outside agents can work in their own
  git worktree/branch, so parallel edits never collide.
- **Custom slash commands** — Markdown prompt templates in `.coded/commands/`.
- **Editable prompts** — the built-in prompts are eight Markdown files
  (`system`, `coder`, `tools`, `security`, `skills`, `planner`, `reviewer`,
  `debugger`) that you can override per user or per project (`coded prompts`).
- **Skill & plugin installs** — `coded skill install` and `coded plugin install`
  from a folder, a `.zip` (path or URL), a git URL or GitHub `owner/repo`;
  plugins bundle skills, commands, hooks, MCP servers and outside agents;
  marketplaces; Claude Code plugins install as-is.
- **Tracing** — JSONL spans for turns, LLM calls and tools (`--trace`), plus
  optional OpenTelemetry export.
- **Prompt-injection guard** — web/PR/MCP output is fenced as untrusted, and
  risky actions after reading it are flagged (or always confirmed, in strict mode).
- **Editor & agent integration** — `coded acp` speaks the Agent Client Protocol
  (Zed, JetBrains, …); `coded mcp-serve` exposes coded as an MCP server.
- **Semantic code search** — build an embedding index (`coded index`) and let
  the agent search the codebase by meaning, not just literal text.
- **Web search & fetch** — `web_search` (Tavily/Brave/SerpAPI/SearXNG) and
  `web_fetch` to read pages, for up-to-date research.
- **Image input** — attach images for vision-capable models (`--image`, `/image`).
- **Checkpoint / undo** — file edits are snapshotted per turn; `/undo` reverts them.
- **Git & GitHub tools** — a `git` tool (read-only runs freely, mutations are
  gated) and a `github` tool (list/get/create PRs, comment) via `GITHUB_TOKEN`.
- **Any OpenAI-compatible endpoint** — configurable base URL, API key, and model
  id, with built-in presets for common providers.
- **Multiple models, switchable** — define models in config, switch live with
  `/model <name>`.
- **Permission layer** — file writes and shell commands ask for approval
  (once / always / deny), or run unattended with `--yolo`.
- **Two front-ends** — a modern inline **REPL** (streaming Markdown, live status
  bar, ghost-text history suggestions, rich completion, framed permission/diff
  prompts) and an optional **full-screen TUI** (`coded --tui`) with a scrolling
  transcript, tool calls and numbered diffs, a status line, and a permission modal.
- **Sub-agents** — the `task` tool spawns a focused agent with its own context
  for large searches or multi-step subtasks.
- **Skills & agent roles** — Claude Code-style `SKILL.md` instruction packs with
  progressive disclosure; the agent auto-invokes them when a task matches.
  Ships with built-in `planner`, `reviewer`, `debugger`, `qa`, and `security` roles.
- **MCP client** — connect Model Context Protocol servers and expose their tools
  to the agent (optional).
- **Context auto-compaction** — long sessions are summarized automatically as
  they approach the model's context window (`/compact` to force it).
- **Session save/resume** — conversations persist to disk; `--continue` /
  `--resume <id>` pick up where you left off.
- **Permission rules** — allow/deny tool actions by pattern in config, plus a
  colored diff preview before every `write`/`edit`.
- **Cost tracking** — `/cost` shows token usage and an estimated dollar cost;
  `--print --output-format json` emits machine-readable results for scripts/CI.
- **Project instructions** — `AGENTS.md` / `CODED.md` / `CLAUDE.md` files are
  loaded from `~/.config/coded/`, the repo root and every directory down to the
  working directory; files in subdirectories load when the agent first works there.

## Install

coded is a normal Python package (3.10+) with a `coded` console script. It runs
on Linux, macOS and Windows — see [Windows](#windows) for what differs there.
Pick whichever of these fits your machine; they all end with `coded --version`
working.

### From a clone (what contributors use)

```bash
git clone https://github.com/krishnavams/coded
cd coded
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .                 # core
pip install -e '.[mcp]'          # + MCP client     (cmd.exe: use ".[mcp]")
pip install -e '.[tui]'          # + full-screen UI
pip install -e '.[dev]'          # + pytest
pip install -e '.[mcp,tui,dev]'  # everything
coded --version
```

`-e` (editable) means your edits take effect without reinstalling.

### As a tool, isolated from your other packages

Best for daily use — one command, its own environment, on your PATH:

```bash
pipx install git+https://github.com/krishnavams/coded          # pipx install pipx first
pipx install "coded[mcp,tui] @ git+https://github.com/krishnavams/coded"
pipx upgrade coded

uv tool install git+https://github.com/krishnavams/coded       # or with uv
uvx --from git+https://github.com/krishnavams/coded coded      # run once, install nothing
```

### Into an existing project or virtualenv

```bash
pip install git+https://github.com/krishnavams/coded
uv pip install git+https://github.com/krishnavams/coded        # uv
poetry add git+https://github.com/krishnavams/coded            # poetry
```

Add a branch or tag with `@name`, e.g. `…/coded@main`. While the repository is
private, `git+https://…` needs credentials — use SSH
(`git+ssh://git@github.com/krishnavams/coded`) or a token, or clone first and
install from the directory.

### Per-platform notes

| Where | Notes |
| --- | --- |
| **Linux** | Install `python3-venv`/`python3-pip` from your package manager first; for the sandbox, `bubblewrap`. Most distributions now block `pip install` outside a virtualenv (PEP 668) — use a venv, `pipx`, or `uv`. |
| **macOS** | `brew install python@3.12 pipx`. The sandbox (`sandbox-exec`) is built in. |
| **Windows** | Install Python from python.org or `winget install Python.Python.3.12`, then use PowerShell or Windows Terminal. Install [Git for Windows](https://git-scm.com/download/win) too: coded uses its Git Bash for the `bash` tool. In `cmd.exe` quote extras with double quotes: `pip install -e ".[mcp]"`. |
| **WSL** | Treat it as Linux; it also gives you the sandbox, which native Windows has no backend for. |
| **Docker** | `python:3.12-slim` has no git, so install it first: `RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*`, then `RUN pip install git+https://github.com/krishnavams/coded`. (Copying the repo in and `pip install .` needs no git.) Pass keys with `-e OPENAI_API_KEY`, mount your project at `/work`, run `coded --cwd /work`. Use `--yolo` only in a throwaway container. |
| **CI (GitHub Actions)** | `pip install git+https://github.com/krishnavams/coded`, then `coded review --fail-on NEEDS_CHANGES` with the key in a repository secret. See `.github/workflows/coded-review.yml`. |
| **Remote box / server** | Same as Linux. For a long session use `tmux`; one-shot work needs no terminal at all: `coded -p "…" --output-format json`. |
| **No internet on the target machine** | On a connected machine with the same Python version and OS: `pip wheel -w wheels .` (coded plus its dependencies, ~24 wheels). Copy `wheels/` over, then `pip install --no-index --find-links wheels coded`. Point `base_url` at your internal endpoint. |

### Check it worked

```bash
coded --version
coded models            # configured models, pools and roles (empty is fine at first)
coded models --check    # sends a tiny request to each endpoint
coded config init       # writes ~/.config/coded/config.json
```

Nothing is needed beyond a key or a local endpoint; see [Configuration](#configuration).

### Uninstall

```bash
pip uninstall coded          # or: pipx uninstall coded / uv tool uninstall coded
rm -rf ~/.config/coded       # settings, skills, plugins, saved sessions
```

## Quick start

The fastest path — just set an API key and go:

```bash
export OPENAI_API_KEY=sk-...
coded                       # start the interactive REPL
coded "explain what this repo does"   # start with an initial prompt
```

Point it at **any** OpenAI-compatible endpoint without a config file:

```bash
# A local Ollama model:
coded --provider ollama --model-name qwen2.5-coder:7b

# Any custom endpoint:
coded --base-url http://localhost:8000/v1 --api-key sk-x --model-name my-model

# Groq:
export GROQ_API_KEY=...
coded --provider groq --model-name llama-3.3-70b-versatile
```

## Configuration

Create a config file with your models:

```bash
coded config setup         # interactive: add, test and wire up models
coded config init          # writes a sample ~/.config/coded/config.json
coded config path          # print the path
coded config show          # show the effective, merged config
```

`coded config setup` (also `/setup` inside the REPL) is the quickest way in. It
asks for the endpoint once, asks the endpoint which models it serves, and lets
you pick several by number; then it names them, sends a test request, and sets
the default model, per-role models, pools and difficulty routing. Keys default
to an **environment variable name** — a literal key is read without echo, never
printed back, and written only to your user config with mode 600. Nothing is
written until you choose "save and quit", the file is replaced atomically, and
keys the wizard doesn't know about (hooks, MCP servers, per-model `limits`)
are left exactly as they were.

Config is layered (later overrides earlier):

1. Built-in provider presets
2. `~/.config/coded/config.json` (or `$CODED_CONFIG`)
3. `./.coded/config.json` (per-project)
4. Command-line flags

A model entry only needs a provider and a model id; the base URL and API-key
environment variable are inherited from the provider preset. Example:

```json
{
  "default_model": "gpt-4o",
  "models": {
    "gpt-4o":     { "provider": "openai",   "model": "gpt-4o" },
    "fast":       { "provider": "groq",     "model": "llama-3.3-70b-versatile" },
    "local":      { "provider": "ollama",   "model": "qwen2.5-coder:7b" }
  }
}
```

**Minimal — just a URL and a model name.** If your endpoint is
OpenAI-compatible, an entry can be as short as a `base_url` and a `model`. No
provider or API key is needed for self-hosted/keyless endpoints (coded falls
back to a placeholder key); add `api_key`/`api_key_env` if yours requires one:

```json
{
  "default_model": "my-model",
  "models": {
    "my-model":   { "base_url": "http://localhost:8000/v1", "model": "my-model-name" },
    "remote":     { "base_url": "https://my-host/v1", "model": "another-model", "api_key_env": "MY_KEY" }
  }
}
```

Or add one for a single run without any config file:

```bash
coded --base-url http://localhost:8000/v1 --model-name my-model-name
```

**Have a lot of models?** [`config.example.json`](./config.example.json) is a
ready-to-trim catalogue with ~25 models across every provider, each with its
own context window, token limit, and pricing filled in. Copy it to your config
path and delete the ones you don't use:

```bash
cp config.example.json ~/.config/coded/config.json
```

### Per-model configuration fields

Each entry under `models` accepts:

| Field | Required | Description |
|---|---|---|
| `provider` | yes* | Provider preset name (supplies `base_url` + key env). |
| `model` | yes | Concrete API model id sent to the endpoint. Defaults to the alias. |
| `base_url` | no | Override the provider's base URL (required for `openai-compatible`). |
| `api_key` | no | Inline key (avoid committing real keys). |
| `api_key_env` | no | Env var holding the key (overrides the provider default). |
| `max_tokens` | no | Max output tokens per response. |
| `temperature` | no | Sampling temperature. Omit for models that reject it (e.g. some reasoning models). |
| `context_window` | no | Total context size (informational; default 128000). |
| `input_cost` | no | USD per **million** input tokens (used by `/cost`). |
| `output_cost` | no | USD per **million** output tokens. |
| `supports_tools` | no | Set `false` for models without function-calling (e.g. search-only models); the agent then runs without tools. |
| `extra_headers` | no | Extra HTTP headers (e.g. OpenRouter's `HTTP-Referer`/`X-Title`). |

\* Instead of `provider`, you may set `base_url` + `api_key`/`api_key_env` directly.

| `verify_ssl` | no | Set `false` to disable TLS certificate verification (insecure; self-signed endpoints). |
| `ca_bundle` | no | Path to a custom CA cert/bundle (PEM) to trust for this endpoint. |

### Self-signed / custom-CA endpoints (TLS)

If your OpenAI-compatible endpoint uses a private CA or a self-signed
certificate, either **trust a CA bundle** (recommended) or **disable
verification** (insecure). Per model in config:

```json
{
  "models": {
    "corp":   { "base_url": "https://llm.internal/v1", "model": "x", "ca_bundle": "/etc/ssl/corp-ca.pem" },
    "selfsigned": { "base_url": "https://192.168.1.5/v1", "model": "x", "verify_ssl": false }
  }
}
```

Or from the CLI for a one-off:

```bash
coded --base-url https://llm.internal/v1 --model-name x --ca-bundle /etc/ssl/corp-ca.pem
coded --base-url https://192.168.1.5/v1  --model-name x --insecure   # skips verification (insecure)
```

`--ca-bundle` / `--insecure` also work on `coded review` and `coded commit`, and
a `CODED_CA_BUNDLE` environment variable is used as a default CA bundle when set.
Disabling verification prints an insecure-connection warning.

### Built-in provider presets

`openai`, `anthropic`, `google` (Gemini), `openrouter`, `groq`, `together`,
`deepseek`, `mistral`, `xai`, `fireworks`, `cerebras`, `perplexity`, `nvidia`,
`deepinfra`, `moonshot`, `ollama`, `lmstudio`, `llamacpp`, `vllm`, `jan`, and
`openai-compatible` (supply your own `base_url`). Each preset knows the default
base URL and the environment variable that holds the API key (e.g. `GROQ_API_KEY`,
`ANTHROPIC_API_KEY`). `anthropic` and `google` use those vendors'
OpenAI-compatible endpoints.

## Usage

```bash
coded [PROMPT]                 # REPL, optionally seeded with a prompt
coded --tui                    # full-screen terminal UI (pip install 'coded[tui]')
coded -p "run the tests"       # non-interactive: run once and print
coded -m fast "..."            # pick a configured model alias
coded --yolo "..."             # auto-approve all tool actions
coded --no-mcp                 # disable MCP servers for this run
coded --continue               # resume the most recent session here
coded --resume <id>            # resume a specific session
coded -p --output-format json "..."   # machine-readable result
coded models                   # list configured models
coded index                    # build the semantic search index
coded review [target]          # non-interactive review pipeline (CI); exit 1 on NEEDS_CHANGES
coded commit [--commit]        # draft a Conventional Commits message from staged changes
coded sessions                 # list saved sessions
```

### Full-screen TUI

`coded --tui` launches a Textual app: a scrolling transcript (user turns,
streamed assistant Markdown, tool calls shown as `● Edit(app.py)` with their
results, line-numbered diffs), the task list while the agent has one, an input
box with a **status line** under it (model, mode, turns, tokens, cost), and a **permission modal** for gated actions. Keys: `Enter` send,
`Ctrl+B` side panel, `Ctrl+L` clear, `Ctrl+U` undo edits, `Ctrl+C` quit.

A **side panel** on the right shows, each part only when it has something:
the session (model, plan mode, how full the context window is and where it
compacts, turns/tokens/cost), the task list with its progress bar, the files
changed this turn with `+added −removed` line counts, messages queued while the
agent works, and background shells with their state. It opens by itself when
the terminal is at least 110 columns wide; `Ctrl+B` opens or closes it at any
width. Files changed by `bash` commands are not listed (they are not tracked
for `/undo` either). It reuses the same agent,
tools, permissions, and session persistence as the REPL. Install the extra:

```bash
pip install -e '.[tui]'      # adds Textual
```

### Slash commands (in the REPL)

| Command | Description |
|---|---|
| `/help` | List commands |
| `/model [name]` | Show or switch the active model |
| `/models` | List configured models |
| `/setup` | Configure models interactively (then reloads the session) |
| `/tools` | List available tools |
| `/skills` | List available skills |
| `/skill <name> [task]` | Invoke a skill now |
| `/review [target]` | Run reviewer→qa→security sub-agents and aggregate |
| `/image <path>... [prompt]` | Attach image(s) for a vision model |
| `/undo` | Revert the last turn's file changes |
| `/checkpoints` | List saved checkpoints |
| `/compact` | Summarize the conversation to free context |
| `/init` | Analyze the repo and write a `CODED.md` guide |
| `/cost` | Token usage and estimated cost |
| `/clear`, `/reset` | Clear the conversation |
| `/config` | Show loaded config files / MCP servers |
| `/plan [request]` | Toggle plan mode (read-only until a plan is approved) |
| `/todos` | Show the agent's task list |
| `/shells [kill <id>]` | List or stop background shells |
| `/agents` | List outside agents available for delegation |
| `/commands` | List custom slash commands |
| `/plugins` | List installed plugins |
| `/prompts` | Show which prompt files are in use |
| `/<custom> [args]` | Run a custom command from `.coded/commands/` |
| `/exit`, `/quit` | Quit |

### Permissions

Tools that change files (`write`, `edit`, `move`, `delete`) or run commands
(`bash`, `git` mutations, `github` writes, MCP tools) ask before running. Answer
`y` (once), `a` (always, for this session), or `n` (deny). Before a `write`/`edit`
you see a colored **diff preview** of the change. Use `--yolo` / `--auto-approve`
to skip prompts, or set `"auto_approve": true` in config. In non-interactive
`--print` mode with no auto-approve, such actions are denied by default (safe).

**Allow/deny rules.** Cut prompt fatigue by pre-approving (or blocking) actions
by pattern. Patterns are `tool` or `tool(glob)` matched against the command/path;
**deny always wins**, even over `--yolo`:

```json
{
  "permissions": {
    "allow": ["bash(git *)", "bash(pytest*)", "read", "grep"],
    "deny":  ["bash(rm -rf*)", "bash(curl*)"]
  }
}
```

### Context & sessions

Auto-compaction is **on by default**: when the conversation reaches 80% of the
model's `context_window` (`compact_ratio`), the older messages are summarized in
place and coded says so ("Summarized earlier conversation to fit context"). The
task list survives it, and the cut is made at a user-message boundary so a tool
call is never split from its result. `/compact` forces it; `{"auto_compact":
false}` turns it off. The threshold follows whichever model answers, so it
shrinks if a smaller fallback takes over — a model with no `context_window` set
is assumed to be 128k.

Long conversations are **auto-compacted**: as the estimated input nears the
model's `context_window`, older messages are summarized into a briefing while a
recent, consistent tail is kept verbatim. Force it with `/compact`; disable with
`--no-compact` or `"auto_compact": false`.

Sessions **persist to disk** (`~/.config/coded/sessions/`, autosaved each turn):

```bash
coded --continue        # resume the most recent session for this directory
coded --resume <id>     # resume a specific one
coded sessions          # list them
```

### Documents: PDF, Word, Excel, PowerPoint

```bash
pip install 'coded[docs]'      # pypdf, openpyxl, python-docx, python-pptx
```

With the extra installed, the `read` tool reads documents as text, paged
through with the same `offset`/`limit` and per-model budgets as source files:

| File | Read as |
|---|---|
| `.pdf` | text per page (`===== page 3 =====`) |
| `.docx` | headings, paragraphs and tables in document order |
| `.xlsx` | each sheet as numbered rows of cell values |
| `.pptx` | per slide — title, bullets, tables and speaker notes |

`pages` picks part of a document first: `"3"`, `"2-5"`, `"1,4,9"`, or a sheet
name. Spreadsheets show the values Excel last calculated; a formula that was
never calculated (a file written by a script) is shown as the formula rather
than a blank.

Out of scope, and reported as such rather than guessed at: **scanned PDFs**
(pages that are images need OCR — the tool says so), charts and pictures inside
Office files, password-protected files, and the pre-2007 binary formats
(`.doc`, `.xls`, `.ppt` — the error gives the `soffice` command to convert
them). Without the extra, reading a document fails with the exact `pip install`
line; it never falls back to decoding a binary file as text.

### Repository map

```
repo_map                      # the whole repo, ranked
repo_map path=src/api         # one subtree
repo_map focus=PermissionManager
```

`repo_map` is an outline of the codebase — each file's classes, functions and
types — with the files **the rest of the code depends on most** first. Grep
answers "where does this string appear"; this answers "what is this made of,
and which part matters", which is the question a model with a small context
window has to answer before it can act.

Ranking is PageRank over a graph whose edges are "this file names a symbol that
file defines", so a small, widely imported module outranks a large one nobody
references. Method names (`run`, `save`) and names defined in many files are
excluded from the graph — otherwise whichever file has the most of them looks
like the heart of the project.

Extraction is Python's own `ast` plus conservative per-language regexes (Go,
Rust, JS/TS, Java/Kotlin, Ruby, PHP, C/C++, Swift, Lua, shell) — no tree-sitter
and no extra dependency, so it is an *outline*, not a parse tree. It lists
definitions only, never code, and everything is bounded: files scanned, bytes
per file, wall time, output. Binary, minified and generated files are skipped.

### Project memory

```
/memory                                   # list
/memory add gotcha: the e2e suite needs Docker
/memory forget m3
```

What a session learns about a repository normally dies with `/clear`. Project
memory keeps a few short notes — conventions, decisions, gotchas, facts — in
`./.coded/memory.json` (gitignored), and puts them in the system prompt at the
start of every future session in that project, labelled as recorded
observations that may be out of date, never as instructions.

The agent can add notes with the `memory` tool, but every write **asks you
first and shows the exact text**, and a turn with untrusted content in context
(web, GitHub or MCP output) cannot write at all — a page should not be able to
plant a durable instruction. The model can only ever write the *project* store,
never your user-level config, so one hostile repository can't follow you
elsewhere. The store is capped, your own notes outlive the agent's, and
`/memory` is the authority: list, add, forget, clear.

### Semantic code search

Build an embedding index of the repo, then the agent's `semantic_search` tool
finds code by meaning. Configure an embedding model, then index:

```json
{
  "embedding": { "provider": "openai", "model": "text-embedding-3-small" }
}
```
```bash
coded index          # build/refresh .coded/index/index.json
coded "where is auth handled?"   # the agent can now semantic_search
```

`embedding` may instead be `"embedding_model": "<alias>"` pointing at a model in
`models`. If neither is set, coded guesses a default embedding model from your
active provider. `grep` is still there for exact/regex matches.

### Web search & fetch

`web_fetch` reads a URL (HTML stripped to text). `web_search` needs a backend —
it auto-detects from whichever is set:

| Backend | Config / env |
|---|---|
| Tavily | `TAVILY_API_KEY` |
| Brave | `BRAVE_API_KEY` |
| SerpAPI | `SERPAPI_API_KEY` |
| SearXNG (keyless) | `SEARXNG_URL` (your instance) |

Or pin it in config: `"web_search": { "backend": "tavily", "api_key": "..." }`.

### Image input (vision)

Attach images for a vision-capable model. Mark the model `"supports_vision": true`,
then:

```bash
coded --image screenshot.png "what's wrong with this UI?"
# or in the REPL:
/image mockup.png build this layout as HTML
```

### Checkpoints & undo

Every turn, changes made by `write`/`edit`/`move`/`delete` are snapshotted under
`.coded/checkpoints/`. `/undo` restores the most recent turn (reverting edits and
removing newly-created files); repeat `/undo` to step further back. `/checkpoints`
lists them. Note: changes made by `bash` are **not** tracked.

### Git & GitHub

The `git` tool runs read-only subcommands (status/diff/log/blame/…) without
prompting and gates mutations (commit/checkout/push/…). The `github` tool needs
`GITHUB_TOKEN` and auto-detects the repo from the `origin` remote:

```bash
export GITHUB_TOKEN=ghp_...
coded "open a PR from this branch to main summarizing the changes"
```

### Skills

Skills are Claude Code-style instruction packs: a directory with a `SKILL.md`
(YAML frontmatter + Markdown body), loaded with **progressive disclosure**.

1. **At startup**, only each skill's `name` + `description` goes into the system
   prompt (cheap), so the model knows what exists and when to use it.
2. When a task matches, the model calls the `skill` tool (or you run
   `/skill <name>`), which loads the **full** `SKILL.md` body into the conversation.
3. The body can point to other files/scripts in the skill folder, which the agent
   reads with `read` or runs with `bash`.

**Built-in role skills.** coded ships with ready-made agent roles that are always
available: **`planner`** (turn a task into an ordered plan), **`reviewer`**
(review a diff for bugs/quality), **`qa`** (write and run tests), and
**`security`** (audit for vulnerabilities). The agent invokes them automatically
when a task matches, or you can run one directly:

```
/skill planner add rate limiting to the API
/skill reviewer            # reviews your current git diff
```

They're written to use the `task` sub-agent tool for isolated work, so a review
or investigation doesn't clutter the main conversation.

**`/review` pipeline.** The `/review [target]` command runs `reviewer`, then
`qa`, then `security` as sequential, isolated sub-agents and aggregates their
reports into one summary. With no target it reviews your current uncommitted
changes (via the `git` tool):

```
/review                       # review the current git diff
/review coded/agent.py        # review a specific file
```

**For CI**, the same pipeline is a non-interactive subcommand that ends with a
synthesized pass/fail verdict and sets the exit code:

```bash
coded review --yolo                       # review the diff; exit 1 if NEEDS_CHANGES
coded review src/ --output review.md      # review a target, save the report
coded review --fail-on never              # always exit 0 (report only)
coded review --no-verdict                 # skip the verdict/gate step
```

`--yolo` lets the qa/security phases run tests and scanners via `bash`
(otherwise those actions are denied in non-interactive mode and the review is
read-only). Example GitHub Actions step:

```yaml
- run: pip install -e . && coded review --yolo -o review.md
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

Discovery locations (a later location overrides an earlier one on name clash, so
your own `planner` shadows the built-in):

```
<package>/builtin_skills/<name>/SKILL.md  # bundled roles (lowest priority)
~/.config/coded/skills/<name>/SKILL.md    # user-global
./.coded/skills/<name>/SKILL.md           # project-local (highest priority)
```

Try the bundled example:

```bash
mkdir -p .coded/skills
cp -r examples/skills/conventional-commit .coded/skills/
coded            # then: /skills
```

A minimal `SKILL.md`:

```markdown
---
name: my-skill
description: What it does, and WHEN to use it (this drives auto-invocation).
allowed-tools: read, edit, bash
---

# My skill
Step-by-step instructions the agent follows once the skill is invoked…
```

See [`examples/skills/`](./examples/skills/) for a working example and a guide.
Use `--no-skills` to disable discovery for a run.

### MCP servers

Add servers under `mcp_servers` in config (install extras with
`pip install -e '.[mcp]'`):

```json
{
  "mcp_servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
    }
  }
}
```

Their tools appear to the agent as `mcp__<server>__<tool>` and go through the
same permission gate.

### Task list & plan mode

For multi-step work the agent keeps a visible task list (`todo_write`), shown in
the REPL as it changes and above the TUI input box (`/todos` to show it again).
Each update shows progress — `Tasks 2/5 ▰▰▰▰▱▱▱▱▱▱ 40%`, the running item marked
`← now` — and the status bar carries the same count and the current item while
the agent works, so a long turn is never a silent one.

**You can type while it works.** A question or follow-up typed mid-turn is
queued (`⏎ queued (1): …`) and run as the next message instead of being
swallowed by the busy terminal; several queue in order. Ctrl-C drops the queue
along with the turn. The permission prompt takes the next line you type, so the
two never fight over the same keystrokes. This works in the REPL and the TUI;
without a terminal (piped input, `--print`) it simply stays off.

**Plan mode** (`coded --plan`, or `/plan` / **Shift+Tab** in the REPL/TUI) makes the agent
research with read-only tools only. When it has a plan it calls
`exit_plan_mode`; you approve (implementation starts) or reject (it revises).
Every risky action is refused while plan mode is on, including `git`/`github`
writes and sub-agents' actions.

### Verification: don't claim success you haven't earned

A turn that changed something can be made to prove it before you see the answer:

```json
{
  "verify": {"command": "pytest -q", "second_opinion": true, "max_rounds": 2},
  "model_roles": {"verify": "big"}
}
```

Two gates, both off until configured:

1. **The project's own checks.** `verify.command` runs after any turn that
   touched something (file tools, `bash`, `git` — not just checkpointed edits).
   A non-zero exit sends the turn back to the agent with the output, telling it
   to fix the cause and **never** to weaken, skip or delete a test — and to say
   so and stop if the failure predates its change.
2. **A second opinion.** With `model_roles.verify` set, a *different* model
   reads your request and the diff and answers `PASS` or `NEEDS_CHANGES`. One
   model marking its own homework is a known weak spot; a reviewer that didn't
   write the code is the cheapest correction available when you have more than
   one endpoint — and in coded they can be from different vendors. The agent is
   told the reviewer may be wrong, so it can push back in its answer instead of
   obeying.

Either complaint costs a round; after `max_rounds` the answer is delivered with
a visible "unverified" note rather than a silent pass. The second opinion runs
only when a verify model is *named* — it is never auto-assigned, so a small
local model is not promoted to critic by accident — and an unparseable verdict
or a reviewer outage releases the turn instead of blocking it. Sub-agents, plan
mode and unchanged turns are never verified, so an unconfigured session sends
exactly the requests it did before.

`/verify` runs the gate on demand, and `coded config setup` offers to configure
it. The command runs through the same shell as the `bash` tool, so on Windows
write it the way that shell expects — Git Bash treats a backslash as an escape,
so `C:/tools/python.exe -m pytest` works where the backslashed path silently
does not.

### Hooks

Run your own commands on agent events. Each hook gets a JSON payload on stdin
(`hook_event_name`, `cwd`, `tool_name`, `tool_input`, `tool_response`, `prompt`).
Exit `2` to **block** (stderr goes back to the model); exit `0` to continue
(stdout of `UserPromptSubmit`/`SessionStart` hooks is added as context). A
`PreToolUse` hook may print `{"decision": "allow"}` to skip the prompt or
`{"decision": "block", "reason": "..."}` to block.

```json
{
  "hooks": {
    "PreToolUse":  [{"matcher": "bash", "command": "./scripts/guard-shell.sh"}],
    "PostToolUse": [{"matcher": "edit|multi_edit|write|apply_patch",
                     "command": "ruff format -q . && ruff check -q . >&2 || exit 2"}],
    "UserPromptSubmit": [{"command": "cat .coded/sprint-notes.md"}],
    "SessionStart": [{"command": "git status --short"}],
    "Stop": [{"command": "pytest -q -x >&2 || exit 2", "timeout": 300}]
  }
}
```

`matcher` is a regex on the tool name (omit for all). The Claude Code form
`{"matcher": ..., "hooks": [{"type": "command", "command": ...}]}` also works. A
`Stop` hook that exits 2 makes the agent keep working (at most 3 times per turn).

### Sandbox

```bash
coded --sandbox                    # bash writes only inside the project; no network
coded --sandbox --sandbox-network  # …but allow network
```

or in config:

```json
{ "sandbox": { "enabled": true, "network": false, "writable": ["~/.cache/pip"],
               "auto_allow": true, "fail_if_unavailable": false } }
```

On Linux this needs [bubblewrap](https://github.com/containers/bubblewrap)
(`apt install bubblewrap`); macOS uses the built-in `sandbox-exec`. Inside the
sandbox the filesystem is read-only except the working directory, `writable`
paths and a private `/tmp`. Because the sandbox is the safety boundary,
sandboxed commands run **without a permission prompt** (`auto_allow`). Deny
rules, plan mode and the strict injection guard still apply. The model can ask
to run one command unsandboxed (`dangerously_disable_sandbox`), which always
needs your approval. If no backend is installed, bash falls back to normal
prompting (or refuses, with `fail_if_unavailable`).

### Windows

coded runs natively on Windows. Everything works except the OS sandbox: there
is no Windows backend, so `--sandbox` falls back to the normal approval prompt
for every command (set `"fail_if_unavailable": true` in the `sandbox` config to
refuse to run instead). WSL gives you the sandbox along with Linux behaviour.

The `bash` tool needs a shell. coded picks one at startup:

1. `"shell"` in config.json, `$CODED_SHELL`, or `--shell`;
2. **Git Bash** (installed with [Git for Windows](https://git-scm.com/download/win))
   — recommended, because it understands the POSIX syntax models write;
3. PowerShell (`pwsh`, then `powershell`), driven with `-EncodedCommand` so
   quoting is never mangled;
4. `cmd.exe`.

```json
{"shell": "pwsh"}
```

```bash
coded --shell "C:/Program Files/Git/bin/bash.exe"
```

The chosen shell is named in the system prompt, and when it isn't POSIX the
model is told to write that shell's syntax (PowerShell needs `&` to run a
quoted executable, `;` to chain, `$env:NAME` for variables). PowerShell's
serialized CLIXML error output is turned back into plain text. `bash.exe` in `System32` is the WSL
launcher, not a shell for the current directory, so it is skipped.

Other Windows specifics, all automatic:

- **Line endings are preserved.** Editing one line of an LF file leaves the rest
  of the file alone (no silent CRLF rewrite), and CRLF files stay CRLF.
- **Output is decoded as UTF-8** from commands, hooks, git and outside agents,
  and the console is switched to UTF-8 so the UI renders.
- **Stopping a command kills its children** (`taskkill /T`), for timeouts,
  `kill_shell` and shutdown.
- **Outside agent CLIs** installed as `.cmd`/`.bat` shims (claude, codex, …) are
  resolved to their real path.

Outside agent CLIs installed as `.cmd`/`.bat` shims are run with the prompt as
an argument, which `cmd.exe` re-parses; coded refuses a prompt containing
`" & | < > ^ %` for such an agent and tells you to set `"stdin": true` for it
(or point `command` at the real `.exe`).

### Background processes

`bash` with `run_in_background: true` starts dev servers, watchers or long
builds without blocking. The agent polls new output with `bash_output` (with an
optional regex `filter`) and stops them with `kill_shell`. `/shells` lists them;
all are stopped when coded exits.

### Multiple endpoints, models and API keys

Every model entry has its own endpoint and key (`base_url`, `api_key`, or
`api_key_env`), so you can mix providers, self-hosted servers and accounts. On
top of that:

```json
{
  "models": {
    "main":   {"provider": "openai", "model": "gpt-4.1", "api_key_env": "OPENAI_KEY_TEAM",
               "fallback": ["main-or", "local"], "max_retries": 4},
    "main-or": {"provider": "openrouter", "model": "openai/gpt-4.1"},
    "cheap":  {"provider": "groq", "model": "llama-3.3-70b-versatile"},
    "local":  {"base_url": "http://localhost:11434/v1", "model": "qwen2.5-coder:14b"},
    "thinker": {"provider": "openai", "model": "o3-mini", "reasoning_effort": "high"}
  },
  "model_roles": {"subagent": "cheap", "compact": "cheap", "review": "thinker", "verdict": "main"}
}
```

- **Retries**: 429/5xx/connection errors are retried with jittered exponential
  backoff (honoring `Retry-After`), `max_retries` / `retry_base_delay` per model.
  A stream that breaks before any text was shown is retried the same way.
- **Timeout**: `"timeout": 900` (seconds) per model: how long to wait for the
  endpoint to connect/respond and, while streaming, between chunks. Unset = 10
  minutes. Raise it for slow, very large models.
- **Fallback**: when a model still fails (or its key is rejected), coded moves
  on to the next alias in `fallback`, e.g. the same model through another
  provider or key, then a local model.
  - **Cooldown**: a model that failed over is skipped for `fallback_cooldown`
    seconds (default 120; `0` = try it first every time), so an outage doesn't
    cost a full retry cycle on every step. It is still tried last if every
    other model in the chain fails.
  - **Smaller fallbacks**: while a fallback with a smaller `context_window` is
    in use, coded sizes tool output and compaction for *that* model and
    shrinks/summarizes the existing conversation to fit. It returns to the main
    model's sizes when the main model is back. Detail is lost in the
    summary, so prefer fallbacks with a similar window.
  - **Typos**: an alias in `fallback`, `model_roles` or `default_model` that
    isn't a configured model is reported at startup and by `coded models`.
- **Roles**: `model_roles` picks the model for sub-agents (`subagent`), context
  compaction (`compact`), the review phases (`review`), the CI pass/fail
  call (`verdict`) and plan mode (`plan`). With `"plan": "large"`, plan mode
  runs on `large` and coded returns to your main model to implement the
  approved plan. The `task` tool also accepts a `model` alias per sub-agent.
- **Health check**: `coded models` lists endpoints, fallbacks and roles;
  `coded models --check [alias ...]` sends a tiny request to each and reports
  latency or the error.
- **Reasoning**: `reasoning_effort` is sent to reasoning models, `extra_body`
  passes provider-specific fields through as-is, and `--show-reasoning` /
  `"show_reasoning": true` displays streamed thinking (`reasoning_content`). An
  optional parameter that an endpoint rejects is dropped automatically and
  remembered.

### Routing by difficulty

Off unless configured. When it is on, coded reads each request and picks the
model for that request — one decision per request, never mid-task:

```json
{
  "routing": {
    "enabled": true,
    "simple": "small",
    "medium": "medium",
    "hard": "large",
    "sticky": true,
    "rules": [{"match": "(?i)^deploy\\b", "level": "hard"}]
  }
}
```

- The classifier is a **heuristic over the text of your request** — keywords
  (refactor, debug, race condition, security, "why does…"), length, and your own
  `rules` (regexes, tried first). It costs nothing and makes no model call.
- A request it can't read — "fix it", "yes", "now the tests" — **keeps the model
  you are already on**, because a short follow-up continues the last task.
- `"sticky": true` (the default) means a session never drops back down after a
  hard request. Set it to `false` to let every request stand on its own.
- coded **never routes down into a model your conversation no longer fits**
  (that would summarize your history to save pennies), and skips a target that
  can't run tools, or can't see images when you attached some.
- `-m` or `/model` **pins** your choice and turns routing off; `/route on` puts
  it back. `/route` alone shows the state, the targets, and the current model.
- Each decision is announced in the session ("Routing: hard request → model
  'large'"), so you can see what it did.

It is a heuristic, not a measurement: treat the levels as a cost control, and
pin the model yourself when a request matters more than it looks.

### Model pools

Several interchangeable models behind one name:

```json
{
  "pools": {"big": ["large-a", "large-b"]},
  "default_model": "big",
  "model_roles": {"subagent": "big"}
}
```

A pool name works anywhere a model alias does — `coded -m big`, `/model big`,
`default_model`, `model_roles`, and routing targets.

- A member is picked when the pool is resolved: at session start, per sub-agent,
  per routed request. The agent then **stays on that member** for the session,
  so the endpoint keeps its prompt cache.
- The other members become its **fallbacks**, ahead of the member's own
  `fallback` list.
- A member that fails is **skipped for everyone** until its cooldown passes
  (`fallback_cooldown`); endpoint health is shared across the process, including
  sub-agents.
- Give members the same `context_window` — `coded models` warns if they differ,
  because each request is sized for the member that was picked.
- `coded models` lists pools; `coded models --check big` checks every member.

### Large and small models

coded adapts to each model's `context_window`. Set it accurately: it's the main
knob, and the default is 128k.

- **Limits scale with the window.** These all grow or shrink in proportion
  (×window/128k, clamped to ⅛×–8×, with floors for small models), so a 300k
  model reads ~4,700 lines per call and keeps ~70k characters of command
  output, while a 16k model gets 250 lines and 4k characters:
  - lines per `read`
  - bash, git and outside-agent output kept
  - `web_fetch` page size
  - grep matches
  - instruction-file budgets
  - messages kept verbatim when compacting

  At 128k the limits are the same as before.
- **Safety caps.** No single tool result may exceed ~30% of the window, and all
  results of one step together ~60%. At 128k that's loose enough for a full
  2,000-line read or a normal batch of parallel reads. `read` stops at whole
  lines and tells the model which `offset` to continue from; other output is
  cut with a note asking for a narrower request. If an endpoint still rejects
  a request as too long, coded halves that step's tool results, compacts and
  retries once. It recognizes "too long" by an HTTP 400 or 413 whose message
  contains phrases such as "context length", "maximum context", "context
  window", "too many tokens", "prompt is too long" or "request too large". If
  your server words it differently, that retry won't fire.
- **Tune per model** with `limits`. Unknown keys are reported at startup:

  ```json
  "limits": {"read_lines": 10000, "shell_output_chars": 200000, "tool_result_chars": 150000}
  ```

  Keys: `read_lines`, `shell_output_chars`, `git_output_chars`, `web_fetch_chars`,
  `grep_matches`, `external_output_chars`, `instructions_file_chars`,
  `instructions_total_chars`, `nested_instructions_chars`, `compact_keep_recent`,
  `tool_result_chars`, `step_result_chars`, `repo_overview_chars`.
- **Prompt sets** (`prompt_profile`):

  | Value | Meaning |
  |---|---|
  | `standard` | The default set |
  | `compact` | Terse, for small or local models; still includes the safety rules |
  | `full` | More thorough `coder`, `planner`, `reviewer` and `debugger` instructions, for large models |
  | `auto` (default) | Picks by context window: ≤32k → compact, ≥200k → full |

  coded can't see parameter counts, so set `prompt_profile` explicitly for a
  700B model with a modest window, or a small model with a huge one.
- **Per-model prompt files.** `prompts_dir` points at a folder of that model's own
  prompt files, which win over everything else. A relative path is resolved
  against the project directory.
- **Longer runs.** `max_turns` per model raises or lowers the step limit; the CLI
  `--max-turns` still wins.
- **Repository overview** (`repo_overview`: `auto`, `true` or `false`; auto means
  on for windows ≥200k). Large-context models get a map of the repository in the
  system prompt: file paths (or directories with counts for big repos), key
  manifests and recent commit subjects, but no file contents. Sub-agents don't
  get it.
- **Switching models** with `/model` rebuilds the system prompt for the new
  model's prompt set and limits. Each sub-agent uses its own model's limits and
  prompt set.
- **Checking the result:**
  - `coded models` shows each model's context size and prompt set.
  - `coded prompts list -m big` shows which file each prompt comes from for that model.
  - `coded prompts show -m big` prints the exact system prompt it gets.

```json
{
  "models": {
    "big":   {"base_url": "https://llm.internal/v1", "model": "my-700b", "api_key_env": "INTERNAL_KEY",
              "context_window": 300000, "max_tokens": 32000, "max_turns": 400,
              "prompt_profile": "full", "limits": {"read_lines": 8000}},
    "local": {"provider": "ollama", "model": "qwen2.5-coder:7b", "context_window": 32768,
              "prompt_profile": "compact"}
  },
  "default_model": "big",
  "model_roles": {"subagent": "local", "compact": "local", "review": "big", "verdict": "big"}
}
```

### Outside agents

If other agent CLIs are installed, the model can hand work to them with the
`external_agent` tool. It's useful for a second opinion, another model's
strengths, or parallel work. Presets (used when the binary is on `PATH`):
`claude` (Claude Code), `codex`, `gemini`, `aider`, `opencode`, `cursor`
(`cursor-agent`), `goose`. Add your own or override presets:

```json
{
  "external_agents": {
    "claude": {"command": ["claude", "-p", "{prompt}", "--model", "opus"]},
    "in-house": {"command": ["./bin/agent", "--stdin"], "stdin": true,
                 "description": "Our deployment agent", "timeout": 3600,
                 "env": {"AGENT_MODE": "ci"}},
    "aider": {"enabled": false}
  }
}
```

Each call needs permission (it runs another program), and can use
`isolation: "worktree"` so the outside agent's edits land on their own branch.
`/agents` shows what's available; `"detect_external_agents": false` turns
auto-detection off.

### Worktree isolation

`task` and `external_agent` accept `isolation: "worktree"`. The work happens in
a fresh git worktree on a new `coded/<label>-<id>` branch under
`~/.cache/coded/worktrees/`, so it can't collide with your working tree or with
other agents. Several isolated tasks requested together run in parallel. If
nothing changed, the worktree and branch are removed. Otherwise the result says
where the changes are, for you to review and merge.

### Prompts

coded's built-in prompts are plain Markdown files in `coded/builtin_prompts/`:

| File | What it covers | Used by |
|---|---|---|
| `system.md` | Identity, tone, output style | main agent |
| `coder.md` | How to do the work: read first, small changes, verify, report | main agent, sub-agents |
| `tools.md` | Tool-calling rules: dedicated tools over bash, parallel calls, which edit tool, background jobs, delegation, handling denials | main agent, sub-agents |
| `security.md` | Untrusted content, secrets, destructive actions, sandbox, refusals | main agent, sub-agents |
| `skills.md` | How to use skills (heads the skill list) | main agent, when skills exist |
| `planner.md` | Planning method | `planner` skill; sent once when plan mode starts |
| `reviewer.md` | Review method | `reviewer` skill, `/review`, `coded review` |
| `debugger.md` | Root-cause debugging method | `debugger` skill |

Override any file by putting your own version in `~/.config/coded/prompts/<name>.md`
(for you) or `./.coded/prompts/<name>.md` (for one project; it wins over the user
one). An override replaces the whole file; an **empty** file removes that section.
Files are used verbatim, so braces and code samples are safe.

```bash
coded prompts list                    # each prompt, where it comes from, size
coded prompts show                    # the assembled main system prompt
coded prompts show subagent           # …or a sub-agent's, or one file: show reviewer
coded prompts init reviewer           # copy the built-in to ~/.config/coded/prompts/ to edit
coded prompts init all --project      # copy all of them into ./.coded/prompts/
```

Overriding `reviewer.md` also changes `/review` and `coded review`, since the
`reviewer` skill reads its instructions from that file (same for `planner` and
`debugger`). Smaller local models work best with short prompts, so trimming
these files is a reasonable thing to do for them.

### Installing skills and plugins

Skills and plugins can be installed from a local folder, a `.zip` file (a path,
for example a file you uploaded, or a URL), any git URL, or GitHub shorthand
`owner/repo[/sub/path][@ref]`.

```bash
coded skill install ./my-skill                      # a folder with SKILL.md
coded skill install ~/Downloads/skills.zip --all    # several skills in one archive
coded skill install acme/agent-skills/skills/lint@v1.2
coded skill list | update [name…] | remove <name> | info <name>

# From skills.sh: paste the page link, or the repository it points to
coded skill install https://skills.sh/vercel-labs/agent-skills/vercel-react-best-practices
coded skill install anthropics/skills --all               # a whole collection
coded skill install anthropics/skills pdf docx            # just some of it
coded skill install https://github.com/anthropics/skills/tree/main/skills/pdf

coded plugin install examples/plugins/code-quality
coded plugin install https://github.com/acme/coded-plugin.git --ref main
coded plugin list | info <name> | update [name…] | enable <name> | disable <name> | remove <name>

coded plugin marketplace add acme/plugin-marketplace   # or a folder / zip / git URL
coded plugin search [query]
coded plugin install code-quality@coded-examples
coded plugin marketplace list | update | remove <name>
```

- **Skill collections (skills.sh and GitHub):** a skills.sh page is a link
  to a GitHub repository, so either link works; a page for one skill installs
  just that skill. Skills are found at the root, under `skills/*/`, `*/`, or
  nested deeper (`plugins/<p>/skills/<name>/`, `.claude/skills/<name>/`).
  Multi-line YAML descriptions and `metadata:` blocks are read correctly. A
  symlink elsewhere in the repository (such as `CLAUDE.md → AGENTS.md`) is
  dropped and ignored; one inside a skill you are installing is still refused.
  With many skills installed, the list in the system prompt is shortened to
  fit (`limits.skills_chars`): descriptions are cut, and past that the rest are
  listed by name — every skill can still be loaded by name.
- **What a plugin can contain:** skills, slash commands (shown as
  `/<plugin>:<command>`), hooks, MCP servers and outside agents. See
  [`examples/plugins/`](./examples/plugins/) for the layout and an example.
  Claude Code plugins (`.claude-plugin/plugin.json`) install as-is:
  - `${CLAUDE_PLUGIN_ROOT}` is expanded.
  - Tool names in hook matchers (`Bash`, `Edit|Write`) are mapped to coded's.
  - Parts coded can't use (sub-agent definitions, HTTP MCP servers, hook events
    such as `SessionEnd`) are listed as warnings.
- **Review before trusting:** before installing or updating, coded prints
  everything the plugin adds:
  - every hook command and MCP server command, verbatim
  - any scripts and executables it contains
  - every warning

  It then asks for confirmation. Without a terminal it refuses unless you pass
  `--yes`. Hooks run without a permission prompt, so installing a plugin lets
  it run code on your machine.
- **Scope:** by default things install for your user (`~/.config/coded/`).
  `--project` installs into `./.coded/` instead. That folder is gitignored, so
  project installs aren't committed.
- **Safe updates:** sources are fetched into a temporary folder and checked
  before anything is replaced. A failed update keeps the previous version.
  Zip archives with `..` paths, symlinks or oversized contents are rejected.
- **Precedence and disabling:**
  - Skill names resolve built-in < plugin < user < project.
  - Plugin hooks are added after your own hooks; they never replace them.
  - On a name clash, your config's MCP servers and outside agents win.
  - `disable` turns off everything a plugin contributes without uninstalling it.
- Changes take effect the next time coded starts. Only you can install, from the
  command line; the agent has no tool for installing skills or plugins.

### Custom slash commands

Save reusable prompts as Markdown in `./.coded/commands/<name>.md` (project) or
`~/.config/coded/commands/<name>.md` (user). Subfolders namespace them
(`commands/git/pr.md` → `/git:pr`).

```markdown
---
description: Fix a GitHub issue end to end
argument-hint: <issue-number>
model: smart            # optional: run this command on another model
---
Read issue #$1 with the github tool, reproduce it, fix it, add a test,
and summarize the change. Extra notes: $ARGUMENTS
```

Run with `/fix-issue 123`. `$ARGUMENTS` is the full argument string and `$1`…`$9`
are individual arguments.

### Tracing & observability

`coded --trace trace.jsonl` (or `"telemetry": {"jsonl": "..."}` or
`$CODED_TRACE_FILE`) writes one JSON span per line for each turn, LLM call
(model, endpoint that answered, tokens, finish reason, duration) and tool call,
linked by `trace_id`/`parent_id`, sub-agents included. With `"otel": true` and
`pip install opentelemetry-sdk opentelemetry-exporter-otlp`, spans are also
exported over OTLP (configure with the standard `OTEL_EXPORTER_OTLP_*` env vars).

### Prompt-injection guard

Output from `web_fetch`, `web_search`, `github` and MCP tools is wrapped in
`<untrusted-content>` and the model is told to treat it as data. Once such
content is in the current turn, `injection_guard` decides what happens to
risky actions:

- `"warn"` (default): permission prompts are flagged "⚠ untrusted content is in context".
- `"strict"`: auto-approval (`--yolo`, allow rules, "always") is suspended, so
  every risky action needs your explicit OK (and is denied when nobody can answer).
- `"off"`: no fencing or flagging.

### Use coded from editors and other agents

- **Agent Client Protocol**: `coded acp` serves ACP over stdio, so ACP-capable
  editors (Zed, JetBrains, Neovim plugins, …) can use coded as their agent:
  streamed replies and thinking, tool-call cards, the task list as a plan,
  permission requests in the editor, `default`/`plan` modes, and cancel. Zed
  example (`settings.json`):

  ```json
  { "agent_servers": { "coded": { "command": "coded", "args": ["acp", "-m", "main"] } } }
  ```

- **MCP server**: `coded mcp-serve` (needs `pip install 'coded[mcp]'`) exposes
  `coded_task(prompt, cwd?, model?)` and `coded_review(target?, cwd?)` to any
  MCP client, e.g. `claude mcp add coded -- coded mcp-serve`. Risky actions are
  denied unless started with `--yolo` (nobody is there to approve them) or
  allowed by permission rules.

## When a model errors

If a turn ends with something like `InternalServerError: Error code: 500`, that
status came from the endpoint. coded names the model and its base URL and
retries first (`max_retries`, with backoff and `Retry-After`), then any
`fallback` models. To find out what the endpoint will actually accept:

```bash
coded models --check glm-5-3-flash
```

The check is a staged probe, not one ping: a gateway that answers "hi" can
still fail on streaming, on `stream_options` or on tool definitions — and some
answer a parameter they don't implement with a 500 rather than a 400, which
looks exactly like an outage. It stops at the first shape refused and says what
to change:

```
✗ glm-5-3-flash         74 ms
      ✓ minimal request
      ✓ streaming
      ✓ stream_options
      ✗ tools: InternalServerError: Error code: 500 - An unexpected error occurred
      → this endpoint cannot take tool definitions. Set "supports_tools": false
        for this model — coded will then run it without tools.
```

The last stage sends the shape a real turn uses (system prompt + streaming +
tools), because each part can pass alone and fail together.

During a turn coded does a narrower version of this by itself: once the retries
for a 5xx are exhausted it tries once more with the optional parameters
(`stream_options`, `reasoning_effort`, `parallel_tool_calls`) removed, says so,
and remembers them as unsupported if that works. It never drops `tools`
automatically — an agent that silently loses its tools is worse than an error.

**If a model doesn't work, coded tries another.** After its own retries, and
after any `fallback` models you declared, it sweeps the rest of your configured
models rather than ending the turn — closest first (same endpoint, then the
widest context window), one attempt each, and it says which one answered. A
model that can't serve the request is skipped: no vision model for an image, no
tool-less model for a turn that needs tools, and nothing whose context window
would force the conversation to be summarized. The error you finally see names
the model *you* chose and lists what else was tried.

This can send a request to a different provider than the one you picked (local
→ cloud, say). To stay strictly on the models you declared:

```json
{"auto_fallback": false}
```

To control the order instead, declare the fallback yourself:

```json
{"models": {"glm-5-3-flash": {"fallback": ["gpt-oss-120b"]}}}
```

## Development

```bash
pip install -e '.[dev]'
pytest
```

The test suite spins up a fake OpenAI-compatible server to exercise the full
agent loop (tool calls, streaming, and the permission gate) without network
access or API keys.

See [`CLAUDE.md`](./CLAUDE.md) for an architecture overview aimed at AI
assistants and contributors.

## License

MIT
