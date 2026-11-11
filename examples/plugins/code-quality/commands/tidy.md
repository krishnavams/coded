---
description: Tidy the files changed on this branch (formatting, dead code, naming)
argument-hint: "[path]"
---
Look at the files changed on this branch (`git diff --name-only` against the default
branch, or just $ARGUMENTS if given). For each one, fix formatting with the project's
own formatter if one is configured, remove dead code and unused imports, and improve
unclear names. Don't change behaviour. Run the tests afterwards and summarize the edits.
