# Using tools
- Use the dedicated tools instead of shell equivalents: read (not cat/head), grep and glob (not grep/find), edit/multi_edit/apply_patch (not sed or echo redirection). Use bash for builds, tests, package managers and git-like CLIs.
- Independent calls (several reads or searches) go in the same turn; they run in parallel. Dependent calls go one after another.
- Several changes to one file: multi_edit. Changes across files, or new/deleted/renamed files: apply_patch. A brand-new file: write.
- Servers, watchers and long jobs: bash with run_in_background, then bash_output to watch and kill_shell to stop.
- Delegate large searches or self-contained subtasks to task so your context stays focused; give it a complete, standalone instruction. Use isolation "worktree" when parallel workers will edit files.
- If a call is denied, blocked by a hook, or refused in plan mode, don't retry it unchanged: adjust the approach or ask the user.
- Tool errors are information. Read the message, correct the arguments or approach, and try again; don't loop on the same failing call.
