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
- **Full tool suite** — `read`, `write`, `edit`, `ls`, `move`, `delete`,
  `glob`, `grep`, `semantic_search`, `bash`, `git`, `github`, `web_search`,
  `web_fetch`, `skill`, and `task` (sub-agents).
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
- **Interactive REPL** — streaming Markdown output, slash commands, input
  history, tab-completion.
- **Sub-agents** — the `task` tool spawns a focused agent with its own context
  for large searches or multi-step subtasks.
- **Skills & agent roles** — Claude Code-style `SKILL.md` instruction packs with
  progressive disclosure; the agent auto-invokes them when a task matches.
  Ships with built-in `planner`, `reviewer`, `qa`, and `security` roles.
- **MCP client** — connect Model Context Protocol servers and expose their tools
  to the agent (optional).
- **Cost tracking** — `/cost` shows token usage and an estimated dollar cost.
- **Project instructions** — a `CODED.md` / `CLAUDE.md` / `AGENTS.md` in the
  working directory is loaded into the system prompt automatically.

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
coded -p "run the tests"       # non-interactive: run once and print
coded -m fast "..."            # pick a configured model alias
coded --yolo "..."             # auto-approve all tool actions
coded --no-mcp                 # disable MCP servers for this run
coded models                   # list configured models
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
| `/image <path>... [prompt]` | Attach image(s) for a vision model |
| `/undo` | Revert the last turn's file changes |
| `/checkpoints` | List saved checkpoints |
| `/cost` | Token usage and estimated cost |
| `/clear`, `/reset` | Clear the conversation |
| `/config` | Show loaded config files / MCP servers |
| `/exit`, `/quit` | Quit |

### Permissions

Tools that change files (`write`, `edit`) or run commands (`bash`, MCP tools)
ask before running. Answer `y` (once), `a` (always, for this session), or `n`
(deny). Use `--yolo` / `--auto-approve` to skip prompts, or set
`"auto_approve": true` in config. In non-interactive `--print` mode with no
auto-approve, such actions are denied by default (safe).

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
