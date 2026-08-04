# Example skills

Skills are Claude Code-style, progressively disclosed instruction packs. Each
skill is a directory with a `SKILL.md` file (YAML frontmatter + Markdown body).

## Install a skill

Copy a skill directory into either location (project skills override user skills
of the same name):

```bash
# Project-local (this repo/working dir):
mkdir -p .coded/skills
cp -r examples/skills/conventional-commit .coded/skills/

# Or user-global:
mkdir -p ~/.config/coded/skills
cp -r examples/skills/conventional-commit ~/.config/coded/skills/
```

coded discovers them on startup. Confirm with `/skills` in the REPL, or
`coded --print "..."`.

## How they load (progressive disclosure)

1. **Startup** — only each skill's `name` + `description` is added to the system
   prompt, so the model knows what exists and when to use it.
2. **On demand** — the model calls the `skill` tool (or you run `/skill <name>`),
   which loads the full `SKILL.md` body into the conversation.
3. **As needed** — the body can reference other files/scripts in the skill
   directory; the agent reads them with `read` or runs them with `bash`.

## Write your own

```
.coded/skills/my-skill/
├── SKILL.md          # required: frontmatter + instructions
├── reference.md      # optional: extra docs the body points to
└── script.py         # optional: code the agent runs with bash
```

`SKILL.md` frontmatter:

```yaml
---
name: my-skill
description: What it does, and WHEN to use it (drives auto-invocation).
allowed-tools: read, edit, bash   # optional, informational
---
```

Write the `description` for triggering — say both what the skill does and the
situations it applies to.
