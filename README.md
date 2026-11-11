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
  transcript, session sidebar, tool/diff cards, and a permission modal.
- **Sub-agents** — the `task` tool spawns a focused agent with its own context
  for large searches or multi-step subtasks.
- **Skills & agent roles** — Claude Code-style `SKILL.md` instruction packs with
  progressive disclosure; the agent auto-invokes them when a task matches.
  Ships with built-in `planner`, `reviewer`, `qa`, and `security` roles.
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

```bash
git clone https://github.com/krishnavams/coded
cd coded
pip install -e .            # core
pip install -e '.[mcp]'     # + Model Context Protocol support
```

Requires Python 3.10+.

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
coded config init          # writes ~/.config/coded/config.json
coded config path          # print the path
coded config show          # show the effective, merged config
```

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
streamed assistant Markdown, tool-call and result cards, syntax-highlighted
diffs), a live **session sidebar** (model, turns, tokens, cost, tools, skills),
an input box, and a **permission modal** for gated actions. Keys: `Enter` send,
`Ctrl+L` clear, `Ctrl+U` undo edits, `Ctrl+C` quit. It reuses the same agent,
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
the REPL as it changes and in the TUI sidebar (`/todos` to show it again).

**Plan mode** (`coded --plan`, or `/plan` in the REPL/TUI) makes the agent
research with read-only tools only. When it has a plan it calls
`exit_plan_mode`; you approve (implementation starts) or reject (it revises).
Every risky action is refused while plan mode is on, including `git`/`github`
writes and sub-agents' actions.

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
- **Fallback**: when a model still fails (or its key is rejected), coded moves
  on to the next alias in `fallback`, e.g. the same model through another
  provider or key, then a local model.
- **Roles**: `model_roles` picks the model for sub-agents (`subagent`), context
  compaction (`compact`), the review phases (`review`) and the CI pass/fail
  call (`verdict`). The `task` tool also accepts a `model` alias per sub-agent.
- **Health check**: `coded models` lists endpoints, fallbacks and roles;
  `coded models --check [alias ...]` sends a tiny request to each and reports
  latency or the error.
- **Reasoning**: `reasoning_effort` is sent to reasoning models, `extra_body`
  passes provider-specific fields through as-is, and `--show-reasoning` /
  `"show_reasoning": true` displays streamed thinking (`reasoning_content`). An
  optional parameter that an endpoint rejects is dropped automatically and
  remembered.

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
