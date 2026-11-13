# Changelog

## Unreleased

### Changed
- The REPL spinner cycles through coded's own words while the model works
  ("Tracing…", "Untangling…", …, a new one every few seconds) and shows how
  long the turn has run; specific labels ("Compacting…") still win.
- Every skill is also a slash command: `/prompt-master write a mail` is
  `/skill prompt-master write a mail` (REPL and TUI), and skills appear in
  slash-command completion. Built-in and custom commands win a name clash.
- Typing `/` while a turn or tool is running lists the matching slash
  commands under the input box, and Tab completes the one being typed (the
  prompt between turns already did this).
- Project memory has an `architecture` kind (which module owns what, how the
  pieces connect), recalled first: `/memory add architecture: …` or the
  agent's `memory` tool.
- A long request no longer stops at `max_turns` (100) while it is still making
  progress: the limit grows by `max_turns` again, up to five times, and stops
  early only when the agent repeats the same tool call or keeps failing. The
  stop message says why and that `continue` picks up where it left off, and it
  is shown even without verbose output. `"auto_extend_turns": false` or an
  explicit `--max-turns` keeps the old hard cap.

### Fixed
- `/dashboard` crashed (`ValueError: too many values to unpack`) whenever the
  session had a task list.
- Text that looks like terminal markup no longer breaks the display. A request,
  task, shell command, memory note, command description or error message
  containing `[/x]` crashed `/help`, `/memory`, `/timeline`, `/dashboard`,
  `/checkpoints`, `/rewind`, `/shells`, `/tasks`, `/commands` and any status
  message; text such as `[name]` was silently dropped, so `/help` showed
  `/model .` instead of `/model [name]` and `pip install 'coded[mcp]'` lost its
  extra. Skill and plugin install summaries are now printed verbatim too.

## 0.3.0

### Security
- **Project trust.** A repository's `.coded/config.json` (and project-scope
  plugins) no longer apply hooks, MCP servers, outside agents, model endpoints,
  allow rules, auto-approve, sandbox or similar settings until you trust the
  project; the REPL/TUI ask once and remember the answer until those settings
  change. `-p`, `coded review`, `coded commit`, ACP and `mcp-serve` ignore them
  (note on stderr) unless `--trust-project` / `CODED_TRUST_PROJECT=1`.
  **If you keep hooks or endpoints in a project config, you will be asked once.**
- Permission rule lists add up across config files; a later file can no longer
  drop a deny rule.
- Reading, listing or writing outside the project (or `--add-dir` folders)
  asks first, symlinks included; accept-edits mode does not skip it.
- `web_fetch` and `browser` ask before localhost, private/link-local/metadata
  addresses and `file://` outside the project, including after redirects.
- `coded commit` no longer auto-approves every tool (git only, with `--commit`,
  deny rules respected); `coded review` honours `injection_guard`.
- `apply_patch` refuses to move onto an existing file; a timed-out MCP call is
  cancelled. The review workflow installs coded from the base branch and no
  longer uses `--yolo`.

### New
- `/heal` self-healing loop, `/refactor`, `repo_map` operations `imports`,
  `dependents`, `impact`, `cycles` and `health`, `fix` memory notes and
  `/memory search`.
- `/dashboard` Command Center, `/timeline` time-travel (and `coded sessions show`),
  `/redteam` and `review --adversarial`, `/twin` project twin, `coded lab` /
  `/lab` self-improvement lab, all fed by a per-session event log.
- Claude Code-style prompts: arrow-key permission prompt with "No, and tell
  coded what to do differently", `ask_user` with up to four questions and a
  review page; folded tool output (ctrl+o / click to expand); new wordmark.

## 0.2.0

### Sub-agents as tasks
- Live progress for each running sub-agent: model, running time, tool uses and
  its latest steps, shown under the spinner; `⎿ Done (…)` when it ends.
- `task` takes `run_in_background`: it returns at once and the report arrives
  as a message when it finishes, while you keep chatting.
- **Ctrl+B** moves a running sub-agent to the background; **Ctrl+T** or
  `/tasks` lists sub-agents and background shells (`/tasks <id>`,
  `/tasks stop <id>`).
- New tools for the model: `task_output` (check a task) and `task_stop`.
- A background task never stops to ask for permission; it reports what it
  could not do.
- TUI: a **Sub-agents** section in the side panel.

### While a turn runs
- An input box stays at the bottom of the screen during a turn, showing what
  you type; Enter queues the message.
- **↑** pulls the newest queued message back into the input to edit it.
- **Shift+Tab** turns accept-edits on or off mid-turn (from the next edit on).
- **Ctrl+O** expands or collapses long tool output.
- `/btw <question>`: a quick side question answered from the conversation,
  even mid-turn, without being added to the history.

### Sessions
- The session is saved after every step, so a crash or kill loses at most one
  step; `--continue` / `--resume` pick it up.
- The task list (todos) is saved with the session and restored on resume.
- Tool calls left unanswered by an interrupted turn are closed, so the history
  stays valid for the endpoint.

### Memory and processes
- Command output, `grep`, `read` and background-shell logs are bounded, so a
  large output can no longer grow coded until the OS kills it.
- A command that times out is stopped together with its child processes.

### Fixes
- A sub-agent stopped with Esc no longer raises a permission prompt afterwards.
- `/btw` asks again without tools when the model replies with a tool call.
- Token and cost totals stay correct with background sub-agents running.
