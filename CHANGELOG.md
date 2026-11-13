# Changelog

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
