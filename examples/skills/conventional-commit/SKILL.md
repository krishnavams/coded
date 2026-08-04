---
name: conventional-commit
description: Write a git commit message in Conventional Commits format from the staged changes. Use when the user asks to commit, write a commit message, or mentions conventional commits.
allowed-tools: bash
---

# Conventional Commit

Produce a well-formed [Conventional Commits](https://www.conventionalcommits.org)
message for the currently staged changes.

## Steps

1. Run `git diff --cached --stat` and `git diff --cached` to see what is staged.
   - If nothing is staged, tell the user to `git add` first and stop.
2. Choose the right **type**:
   `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`,
   `chore`, or `revert`.
3. Choose an optional **scope** (the affected module/area), lowercase.
4. Write the message:

   ```
   <type>(<scope>): <short imperative summary, ≤72 chars>

   <body: what changed and why, wrapped at ~72 cols>
   ```

   - Use `!` after the type/scope and a `BREAKING CHANGE:` footer for breaking changes.
   - Do not include file lists that git already tracks.

5. Show the proposed message to the user. Only run `git commit` if they ask you to.

## Notes
- Keep the summary in the imperative mood ("add", not "added").
- Prefer one logical change per commit; if the diff mixes concerns, say so.
