# Security remediation plan

Scope: the existing coded agent (no rebuild). Each finding was confirmed by
reading the code path before it was changed; the outcome of each is in
`SECURITY_AUDIT_REPORT.md`.

**Baseline** (before any change, commit `776fae6`, Linux, Python 3.13):
`pytest` → 785 passed, 14 skipped (Windows-only shells, no bubblewrap, no
Playwright, no `build` module).

**Existing protections verified first, and kept:** the permission gate (deny
rules win over `--yolo`; unattended runs deny), project trust for
`.coded/config.json`, outside-project path prompts (symlinks resolved),
`.codedignore`, untrusted-output fencing + injection guard, a per-request
URL check in `web_fetch`/`browser` with re-checked redirects, the bash
sandbox, bounded output and file reads, atomic session saves, `close_tool_calls`
on load, background tasks that cannot prompt.

## Findings and plan

| ID | Source | Root cause | Exploit scenario | Fix | Regression tests | Break risk |
|---|---|---|---|---|---|---|
| SEC-01 | `mcp_client.server_params` | A server with any `env` got `{**os.environ, **env}` | A third-party MCP server (or one compromised later) reads `OPENAI_API_KEY`, `AWS_*`, `GITHUB_TOKEN` from its environment | `child_env`: allow-list (`SAFE_ENV`, `LC_*`) + the server's own `env`; `inherit_env: true / [names]` to opt in | `test_security.py::test_mcp_*` incl. a real MCP child process | A server relying on an undeclared variable must now list it (`env` / `inherit_env`) — documented |
| SEC-02 | `sessions_store.SessionStore.save/load`, `mission.save_*`, `trust.remember/forget` | `Path.write_text` under the default umask → 0644 files in a 0755 folder; session id joined into a path unchecked | Another local account reads whole conversations (code, secrets a tool printed); `--resume ../x` reads any JSON file | `securefs`: 0700 folder, 0600 exclusive temp file, atomic rename, symlinks refused, `check_id`; old readable files tightened | `test_sessions_are_private_*`, `test_the_temp_file_is_private_*`, `test_an_old_readable_*`, `test_session_ids_cannot_leave_*`, `test_a_symlinked_*`, `test_existing_sessions_still_load` | Low: same format and paths |
| SEC-03 | `agent._run_tool_inner`, `tools/shell.BashTool` | `dangerously_disable_sandbox` only turned off `auto_allow`; the ordinary `bash` approval (allow rule, "always", hook allow) then authorized running unsandboxed. No backend + `fail_if_unavailable: false` ran unsandboxed silently | A mission with `--allow-bash` (rule `bash`) or a user who said "always" to bash: a prompt-injected model sets the flag and writes outside the project / reaches the network without a question | `Tool.escalation()`; bash returns a `bash_unsandboxed` question asked with `always_ask`, never answered by bash's rules/always/hooks/auto-allow; deny rules for bash still win; `escalation` event; output labelled | `test_ordinary_bash_approval_does_not_cover_*`, `test_explicit_bypass_*`, `test_hooks_and_unattended_runs_cannot_bypass`, `test_a_missing_sandbox_backend_*` | Unattended runs that used to bypass now refuse (intended); `--yolo` still allows |
| SEC-04 | `netguard.host_is_internal` (`lru_cache`), `websearch.fetch_url` | The name was resolved for the check, then httpx resolved it again to connect; answers were cached forever | DNS rebinding: `evil.example` answers 93.184.x.x for the check and 169.254.169.254 for the connection → cloud credentials | `pinned_transport`: one resolution per request (redirects included), judged, connection to that address, hostname kept for Host/SNI/cert; no keep-alive; no DNS cache; NAT64/6to4/CGNAT internal | `test_dns_rebinding_*`, `test_the_checked_address_*`, `test_a_redirect_to_a_private_*`, `test_tls_keeps_the_hostname_*`, `test_internal_destinations_*`, `test_public_destinations_pass`, `test_a_public_page_still_loads_*` | Behind a proxy the old check stays (proxy resolves) — documented |
| SEC-05 | `tools/browser.BrowserTool.run` | Routing was set on the first page only; service workers bypass routing; downloads allowed | A page opens a pop-up or registers a service worker that requests `http://127.0.0.1:…/admin/delete` unchecked | `context.route` (all pages), `service_workers="block"`, `accept_downloads=False`, pop-ups closed and reported | `test_browser_popups_and_subrequests_*` (real Chromium) | Low |
| SEC-06 | `Agent.run`, `SessionKeeper`, `mission._run` | No guard against two turns in one agent, or two processes in one session | Two terminals `--resume` the same session (or a mission resumed twice) interleave turns and overwrite each other's saves; a mission runs twice in one worktree | Per-agent turn lock (`AgentBusy`); per-session lock file with stale-pid takeover (`Busy`) | `test_concurrency.py` | A second process resuming a busy session is refused (message names the pid) |
| SEC-07 | `tools/task._work` | `finalize_worktree` skipped on `BaseException` | Esc during a worktree task leaves a worktree + branch behind | `finally` | `test_an_interrupted_worktree_task_*` | None |
| SEC-08 | events, telemetry, `/export`, mission report + `--notify-url`, `explain_failure` | Text went out verbatim | A token printed by a command ends up in a report POSTed to a public ntfy topic, or in a shared export | `redact.py` applied at those sinks | `test_secrets_are_redacted`, `test_ordinary_text_is_left_alone`, `test_environment_secret_values_*`, `test_redaction_reaches_*` | Exports/reports show `[REDACTED]` (intended) |
| SEC-09 | `agent._run_tool_inner` | Arguments were JSON-parsed only; tools did their own checks | Malformed arguments reach tools (KeyError text, wrong types) | `validate_arguments` (required, types with forgiving coercion, enums, `additionalProperties: false`), 8 MiB cap | `test_malformed_arguments_*`, `test_common_model_slips_*`, `test_oversized_arguments_*` | Nested shapes deliberately not checked (ask_user accepts loose forms) |
| SEC-10 | file tools | (verification) | symlink / `../` escape, binary/non-UTF-8 edits, stale edits, permissions, `.codedignore`, large files | No change needed: existing checks hold | `test_a_symlink_inside_*`, `test_edits_refuse_*`, `test_an_edit_after_*`, `test_write_creates_*`, `test_codedignore_*`, `test_a_large_file_*` | — |

## Phases

1. Baseline; SEC-01, SEC-02, SEC-03 + tests. 2. SEC-04, SEC-05 + tests.
3. SEC-06, SEC-07 (`EXECUTION_LIFECYCLE.md`). 4. SEC-08, SEC-09, SEC-10.
5. Documentation, cross-platform review, final audit. Each phase ended with the
full suite green and its own commit.
