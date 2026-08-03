# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

> **Status: empty repository.** As of this file's creation, `krishnavams/coded`
> contains no source code, build tooling, or history. This document is a
> scaffold — a set of sections to fill in as the project takes shape. When you
> add real code, replace the placeholder guidance below with concrete facts
> (actual commands, real directory names, verified conventions). Do not leave
> invented details in place.

## How to keep this file accurate

When updating this file, prefer facts you can verify from the repository over
assumptions:

- **Commands**: only document build/test/lint commands that actually exist
  (check `package.json` scripts, `Makefile`, `pyproject.toml`, etc.).
- **Structure**: describe directories that are actually present.
- **Conventions**: derive them from existing code, linter configs, and
  formatter settings rather than stating generic best practices.

If a section does not yet apply, leave its heading with a short "not yet
established" note so the structure is preserved for later.

## Project overview

_Not yet established._ Describe what this project does, who it's for, and the
core problem it solves once there is code to describe.

## Tech stack

_Not yet established._ Record the language(s), framework(s), runtime versions,
and package manager once chosen (e.g. from `package.json`, `go.mod`,
`requirements.txt`, `Cargo.toml`).

## Repository structure

_Not yet established._ Map the top-level directories and what lives in each once
the layout exists. Example format to fill in later:

```
src/        # application source
tests/      # test suite
...
```

## Development workflow

### Setup

_Not yet established._ Document how to install dependencies and prepare a local
environment.

### Build

_Not yet established._ Document the build command(s).

### Test

_Not yet established._ Document how to run the test suite (and a single test)
once tests exist.

### Lint / format

_Not yet established._ Document linting and formatting commands and the tools
they use.

## Coding conventions

_Not yet established._ Capture naming, style, error-handling, and structural
conventions once patterns emerge in the code. Match the style of surrounding
code when making changes.

## Git & branching

- The default working branch for AI-assisted changes in this session is
  `claude/claude-md-docs-n4n6zc`.
- Use clear, descriptive commit messages.
- Do not open a pull request unless explicitly asked.

## Notes for AI assistants

- This repository currently has no code. Before claiming any command,
  dependency, or convention exists, verify it against the actual files.
- Keep this file in sync with reality: update it whenever the project's
  structure, tooling, or conventions change.
