---
name: changelog
description: Write or update CHANGELOG.md entries from recent commits. Use when the user asks for a changelog, release notes, or "what changed since <version>".
---
# Changelog

1. Find the last release: `git describe --tags --abbrev=0` (if there are no tags, use the first commit).
2. List changes since then: `git log <last>..HEAD --pretty=format:'%h %s'`.
3. Group them under Keep a Changelog headings: Added, Changed, Fixed, Removed, Security.
   Skip merge commits and pure chores unless the user wants them.
4. Write each entry for users, not developers: what changed and why it matters.
5. Prepend a new `## [Unreleased]` section to CHANGELOG.md (create the file if missing),
   leaving existing entries untouched. Show the user the new section.
