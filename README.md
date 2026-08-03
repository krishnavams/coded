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
- **Full tool suite** — `read`, `write`, `edit`, `ls`, `glob`, `grep`, `bash`,
  and `task` (sub-agents).
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
environment variable are inherited from the provider preset. See
[`config.example.json`](./config.example.json). Example:

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

### Built-in provider presets

`openai`, `openrouter`, `groq`, `together`, `deepseek`, `mistral`, `xai`,
`fireworks`, `ollama`, `lmstudio`, `llamacpp`, and `openai-compatible`
(supply your own `base_url`). Each preset knows the default base URL and the
environment variable that holds the API key (e.g. `GROQ_API_KEY`).

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
