# Execution lifecycle

How a unit of work in coded is created, runs, waits, ends and recovers — and
which existing object owns each part. This release did **not** add a new
execution engine: the four existing owners (Agent turn, sub-agent task,
background shell, mission) already carry ids, states, cancellation and saved
recovery data. The gaps found were concurrency and cleanup, and those were
fixed in place (see "What changed").

## The units of work

| Unit | Id | Owner | Parent |
|---|---|---|---|
| Session (one conversation) | `20261115-073303-123` | `SessionStore` / `SessionKeeper` | — |
| Turn (one request to completion) | the user message, by identity (`Agent._turn_marks`) | `Agent.run` | session |
| Sub-agent task | `t1`, `t2`, … (`TaskEntry.id`) | `TaskManager` | the turn that called `task` |
| Background shell | `bg1`, … | `BackgroundShells` | the session's agent |
| Mission | `20261114-185258` (`.coded/missions/<id>.json`) | `mission.run_mission` | owns one session |

Every tool call, verify round, escalation, denial, interrupt and step-limit
stop is an event in the session's `EventLog` (`events.py`), tagged with the
sub-agent `task` id when it happened inside one. That log is the structured
execution record: `/dashboard`, `/timeline` and `coded lab` read it, and it
is saved with the session.

## States, and where each lives

The suggested lifecycle maps onto existing state like this:

| Lifecycle state | Turn (REPL/TUI: `execstate.ExecState`) | Task (`TaskEntry.status`) | Mission (`state["status"]`) |
|---|---|---|---|
| CREATED | — | `TaskManager.start` | `_start` writes the state file |
| QUEUED | follow-up / queued message (`FOLLOW_UP_PENDING`) | — | — |
| RUNNING | `COMMAND_RUNNING`, `TOOL_EXECUTING` | `running` | `running` |
| WAITING_FOR_APPROVAL | `WAITING_FOR_USER_INPUT` (`Execution.waiting()`) | never: background tasks are refused, not asked | never: nobody to ask |
| VERIFYING | `verify` events (`Agent._verify_turn`) | — (sub-agents are not verified) | inside each run |
| COMPLETED | `COMPLETED` | `completed` | `done` |
| FAILED | `FAILED` | `failed` | `failed`, `stuck`, `budget`, `unfinished`, `unverified` |
| CANCELLED | `INTERRUPTED` | `stopped` | `interrupted` |
| TIMED_OUT | a command: `TimeoutExpired` → tool error; a turn: `max_turns` event | — | `stuck` (not progressing) |
| RECOVERY_REQUIRED | session saved per step; orphan tool calls closed on load | — | `running` left in the state file after a kill → `--resume` |

## Interactive vs unattended

`PermissionManager` decides, per thread:

- **Interactive** (REPL/TUI main turn): risky actions ask.
- **Unattended** (no prompt: `--print`, CI, missions) and **background**
  (`set_background`: a `run_in_background` task, a task moved there with
  Ctrl+B, or a stopped task finishing its step): `request()` denies with
  reason `unattended`, `ask_user` gets None. A background task can never
  obtain an approval, including the UNSANDBOXED escalation.

## Cancellation

- REPL: Esc/Ctrl-C send SIGINT to the main thread; the turn unwinds.
- TUI: `Agent.interrupt()` + `Agent.cancellable`: the model call is abandoned,
  `bash` gets `ctx.extra["cancel"]` and its process tree is killed, a
  foreground task is stopped.
- Tasks: `TaskManager.stop` interrupts the sub-agent at its next step.
- Background shells are killed at exit (`atexit` → `kill_all`).
- Missions: Esc = Ctrl-C → status `interrupted`, resumable.

## Recovery without duplicate work

- The session is saved atomically after the user message, every tool step
  and the answer (`Agent.on_step`). A kill loses at most one step.
- On load, tool calls with no result get `INTERRUPTED_RESULT`; they are
  **not re-run**. The model sees "interrupted" and decides; destructive
  operations are never replayed automatically.
- A mission resumes from its saved task list (no second planning run).

## What changed in this release

1. **One turn at a time per agent.** `Agent.run` holds a per-agent lock; a
   concurrent call raises `AgentBusy` instead of interleaving two turns into
   one history. Released on errors and interrupts.
2. **One process per session.** `SessionStore.lock(id)` is a lock file
   (`.<id>.lock`: pid + host) beside the session. `--resume`/`--continue`,
   `/resume` and missions claim it; a session a live process holds is refused
   with its pid. A lock whose process is gone is taken over (liveness is
   checked without signals; on Windows via `OpenProcess`). A lock from another
   host (shared folder) is respected. This also stops a second
   `coded mission --resume` of a mission that is still running.
3. **Saves never corrupt.** Each save writes a uniquely named 0600 temp file
   and renames it over the session; concurrent writers cannot interleave bytes.
4. **No orphan worktrees.** `task` with `isolation: "worktree"` finalizes its
   worktree in a `finally`: an untouched one is removed and a changed one kept
   and described, also when the sub-agent was interrupted.

## Not done, and why

- A single cross-cutting execution object with its own persisted state
  machine. The existing owners already persist what recovery needs; a parallel
  engine would duplicate state that must then be kept in sync, which is the
  kind of change most likely to break resume. Proposal if needed later: give
  `EventLog` `run_start`/`run_end` events with a uuid and parent uuid, emitted
  by `Agent.run` and `TaskManager.start/finish`; no behaviour change.
- Reaping a sub-agent's own child processes when the task is stopped. They
  share the parent's `BackgroundShells`, which are killed at exit; a stopped
  task's foreground command is killed via the cancel event (TUI) or SIGINT
  (REPL).
