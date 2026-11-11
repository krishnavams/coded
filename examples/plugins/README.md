# Example plugins

A plugin bundles skills, slash commands, hooks, MCP servers and outside agents
into one installable directory.

```bash
# Install straight from this folder:
coded plugin install examples/plugins/code-quality

# Or add the examples folder as a marketplace and install by name:
coded plugin marketplace add examples
coded plugin search
coded plugin install code-quality@coded-examples
```

## Layout

```
my-plugin/
├── .coded-plugin/plugin.json   # manifest (or .claude-plugin/plugin.json)
├── skills/<name>/SKILL.md      # skills
├── commands/<name>.md          # slash commands → /my-plugin:<name>
├── hooks/hooks.json            # {"hooks": {"PostToolUse": [...]}}
└── .mcp.json                   # {"mcpServers": {"name": {"command": ..., "args": [...]}}}
```

Manifest fields: `name` (required, lowercase), `version`, `description`,
`author`, `license`, and optionally `hooks` / `mcpServers` (inline objects or
paths), `skills` / `commands` (extra directories), and `external_agents`
(same shape as the config key).

Use `${CODED_PLUGIN_ROOT}` (or `${CLAUDE_PLUGIN_ROOT}`) in hook and MCP commands
to refer to the installed plugin directory. Claude Code plugins
(`.claude-plugin/plugin.json`) install as-is: tool names in their hook matchers
(`Bash`, `Edit|Write`) are mapped to coded's, and unsupported parts (sub-agent
definitions, non-stdio MCP servers, unknown hook events) are reported at install time.

A marketplace is any directory or repo with `.claude-plugin/marketplace.json`
(or `.coded-plugin/marketplace.json`) listing `plugins`, each with a `name` and a
`source`: a relative path (`./plugins/x`), `{"source": "github", "repo": "o/r"}`,
or `{"source": "url", "url": "https://….git"}`.
