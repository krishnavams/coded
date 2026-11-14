# Changelog

## 0.4.0

### Added
- `coded mission "<goal>"`: an unattended run. It asks everything up front
  (trust, the project's checks, shell commands), works in its own git worktree
  and branch, plans with the task list, retries model outages, continues after
  the step limit or with tasks left open (`--max-restarts`), respects
  `--budget`, commits its changes on the branch and ends with a Markdown report
  in `.coded/missions/` plus a desktop notification or a POST to `--notify-url`.
- The `max_turns` event records why the run stopped (`reason`).
- Missions plan first: the first run may only read and must write the task
  list before any change is allowed. `coded mission --resume [id]` carries on
  with a mission that was killed (sleep, restart, closed terminal) and still
  writes the report; `coded mission --list` shows them.
- `/mission <goal>` in the REPL starts a mission with the session's model
  and folder (`/mission --resume`, `/mission --list`).
- `/resume [n | id]` (REPL and TUI): list this folder's saved conversations and
  switch to one without restarting; the one you leave is saved first, the one
  you open keeps saving under its own id.
- Command suggestions as you type `/`, in the REPL prompt, the box shown while
  the agent works and the TUI: subcommands, flags and live values (models,
  styles, sessions, requests, tasks, shells, memory notes, permission rules,
  files, `@paths`); fuzzy and typo-tolerant matching with the matched letters
  highlighted; ranked by what this project uses and by what the session points
  at next (failed checks → `/heal`, edits → `/review`, …). ↑/↓ choose, Tab or
  Enter takes, Esc closes; a suggestion never runs by itself. Unknown commands
  get "Did you mean …?". Mouse in the TUI; in the REPL with `"mouse": true`.
- Messages typed while the agent works reach the *running* agent at its next
  step (or before it would finish) instead of waiting for the turn to end;
  read-only commands (`/btw`, `/dashboard`, `/tasks`, `/todos`, `/cost`,
  `/context`, `/help`) answer at once, other commands run after.
- Execution state shown apart from the input: running, running tool, waiting
  for you, follow-up pending, interrupting, and how the last request ended
  (completed / failed / interrupted) — in the dock, the TUI status line and the
  REPL status bar.
- While the agent works: ↑/↓ walk earlier messages, Ctrl+R searches them,
  Ctrl+L clears the screen; the typed command, flags, values and `@paths` are
  highlighted (also in the prompt between turns).
- Esc stops a running turn in the TUI at once: a model reply that is still
  coming (or slow to start) is cut off and a running shell command is killed
  with its child processes, as well as a sub-agent the turn waits on.
- `docs/coded-documentation.pdf`: all the documentation in one PDF
  (`python docs/build_pdf.py` rebuilds it).

### Changed
- The REPL spinner cycles through coded's own words while the model works
  ("Tracing…", "Untangling…", …, a new one every few seconds) and shows how
  long the turn has run; specific labels ("Compacting…") still win.
- Every skill is also a slash command: `/prompt-master write a mail` is
  `/skill prompt-master write a mail` (REPL and TUI), and skills appear in
  slash-command completion. Built-in and custom commands win a name clash.
- The input box shown while the agent works is a real line editor: ←/→,
  Home/End, Delete, word jumps (Ctrl/Option+←/→), Shift-selection, Ctrl+A/E/K/U/W,
  and multi-line messages (`\`+Enter, Alt+Enter; Ctrl+Enter on Windows).
- A completed command ends with a space, ready for its arguments.
- The questions asked before work starts — trust this project, a mission's
  checks and shell commands, installing a skill or plugin, `/lab apply`, the
  setup wizard's yes/no — use the same panel as permission prompts (↑/↓,
  Enter, Esc = no) instead of a bare `[y/N]` line.
- The TUI no longer sends an unknown `/word` to the model; it suggests a
  correction (or points REPL-only commands to the REPL). Paths such as
  `/etc/hosts …` still go to the model.
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
- Arrow keys, Home/End and Delete did nothing in the input box shown while the
  agent works: it had no cursor and dropped every escape sequence.
- `/review`, `/redteam`, `/refactor`, `/init`, `/compact`, `/verify`,
  `/plan <task>`, `/image … <prompt>`, skills and custom commands ran with no
  input at all: keys echoed raw into the output, Esc did nothing and typing was
  lost. They now keep the input box like any request.
- Ctrl-C / Esc used to discard messages typed during the turn; they now go back
  into the input.
- With many sub-agents running, the input box was pushed off the screen
  (shown as "..."); the dock now fits the terminal.
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
