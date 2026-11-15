# Security audit report

coded 0.5.0. Plan, root causes and exploit scenarios: `SECURITY_REMEDIATION_PLAN.md`.
Execution and concurrency design: `EXECUTION_LIFECYCLE.md`.

## Summary

| ID | Finding | Severity | Status | Files | Tests |
|---|---|---|---|---|---|
| SEC-01 | MCP servers inherited coded's whole environment | HIGH | Fixed | `coded/mcp_client.py` | `tests/test_security.py` |
| SEC-03 | Ordinary bash approval also authorized running outside the sandbox; a missing backend ran unsandboxed silently | HIGH | Fixed | `coded/tools/shell.py`, `coded/tools/base.py`, `coded/agent.py`, `coded/permissions.py` | `tests/test_security.py` |
| SEC-04 | DNS rebinding window in `web_fetch`; permanent DNS cache; some embedded-IPv4 / CGNAT ranges counted as public | HIGH | Fixed (no proxy); mitigated (behind a proxy) | `coded/netguard.py`, `coded/websearch.py`, `coded/tools/web.py` | `tests/test_security.py`, `tests/test_trust.py` |
| SEC-02 | Sessions, mission files and the trust store readable by other local accounts; session id path traversal | MEDIUM | Fixed (POSIX); documented (Windows) | `coded/securefs.py`, `coded/sessions_store.py`, `coded/mission.py`, `coded/trust.py` | `tests/test_security.py` |
| SEC-05 | Browser pop-ups and service workers bypassed request checks; downloads allowed | MEDIUM | Fixed; DNS-rebinding window narrowed, not closed | `coded/tools/browser.py` | `tests/test_security.py`, `tests/test_trust.py` (real Chromium) |
| SEC-06 | Concurrent turns in one agent / two processes in one session interleaved and overwrote each other | MEDIUM | Fixed | `coded/agent.py`, `coded/securefs.py`, `coded/sessions_store.py`, `coded/cli.py`, `coded/mission.py` | `tests/test_concurrency.py` |
| SEC-08 | Secrets reached the event log, telemetry, exports, mission reports/notifications and error text verbatim | MEDIUM | Fixed | `coded/redact.py`, `coded/events.py`, `coded/telemetry.py`, `coded/transcript.py`, `coded/mission.py`, `coded/llm.py` | `tests/test_security.py` |
| SEC-07 | Interrupted worktree task left an orphan worktree and branch | LOW | Fixed | `coded/tools/task.py` | `tests/test_concurrency.py` |
| SEC-09 | Tool arguments not validated against the schema | LOW | Fixed | `coded/tools/base.py`, `coded/agent.py` | `tests/test_security.py` |
| SEC-10 | Filesystem edge cases (symlink/`../` escape, binary, races, permissions, ignore, size) | INFORMATIONAL | Verified; no change needed | — | `tests/test_security.py` |

## Details

### SEC-01 — MCP environment (HIGH, fixed)
**What was wrong.** `server_params` returned `{**os.environ, **cfg["env"]}` for
any server with an `env` block, so the child saw every model API key, cloud
credential and token coded had. (Servers without `env` used the MCP SDK's own
minimal default, so the leak depended on configuration.)
**Exploitable.** Yes, by any MCP server code (malicious package, compromised
update). Project-level servers already needed project trust; user-level ones
did not.
**Change.** `child_env`: an allow-list (`PATH`, `HOME`, user, locale, temp,
proxy/CA, the Windows start-up variables) plus the server's own `env`, which
wins; `${VAR}` still reads coded's environment explicitly. `"inherit_env":
true | ["NAME", …]` widens it. Exported shell functions are never passed.
**Verified.** Unit tests on the computed environment, and a real MCP server
child that lists its environment: an explicit variable and `PATH` arrive,
`OPENAI_API_KEY` set in the parent does not.
**Limits.** Proxy URLs (which may embed credentials) are passed because
servers need them to reach the network.

### SEC-03 — Sandbox bypass (HIGH, fixed)
**What was wrong.** With the sandbox on, `dangerously_disable_sandbox: true`
only removed sandbox auto-allow; the request then went through the normal
`bash` gate, so a `bash` allow rule (missions add one with `--allow-bash`),
an earlier "always", or a hook's allow approved an unsandboxed run with no
question. With no backend installed and `fail_if_unavailable: false`, commands
ran unsandboxed with nothing saying so.
**Exploitable.** Yes: prompt injection in an unattended mission with
`--allow-bash` could escape the sandbox.
**Change.** `Tool.escalation()`; for bash outside an enabled sandbox it is a
separate `bash_unsandboxed` request, `always_ask`, auto-allow off. The prompt
says UNSANDBOXED, shows the command, and that filesystem and network limits do
not apply. bash deny rules still win. Unattended/background runs refuse it.
`--yolo` and an explicit `bash_unsandboxed` allow rule are the deliberate ways
to pre-approve. Each decision is an `escalation` event. Output carries
`[UNSANDBOXED: …]`.
**Verified.** Agent-level tests with a recording prompt: an allow rule +
"always" for bash still ask the UNSANDBOXED question and a "no" prevents the
write; "yes" runs it and labels it; unattended, background and deny-rule cases
refuse. Run with a real bubblewrap backend.

### SEC-04 — SSRF / DNS rebinding in web_fetch (HIGH, fixed / mitigated)
**What was wrong.** `host_is_internal` resolved the name and cached the answer
for the process (`lru_cache`); httpx then resolved again to connect. A
rebinding name could pass as public and connect to 127.0.0.1 or
169.254.169.254. NAT64 (`64:ff9b::/96`), 6to4 and `100.64.0.0/10` were
public. Old-style IPv4 hosts (`127.1`, `0x7f.1`, `2130706433`), which
browsers turn into 127.0.0.1, were judged by the resolver, and Windows'
resolver does not expand them.
**Exploitable.** Yes, with an attacker-controlled domain and a prompt-injected
fetch.
**Change.** `netguard.pinned_transport`: per request (each redirect too) one
resolution → judged → the URL host replaced by that IP, `Host` header and TLS
SNI/certificate name kept as the original host (httpcore `sni_hostname`); no
keep-alive so a pooled connection cannot carry another host's request. No DNS
cache. Legacy IPv4 forms are parsed with WHATWG URL rules, independent of
the resolver. Approval of an internal destination is by the caller's judgement
(`allow(url, reason)`), not a second lookup.
**Verified.** A fake resolver that answers public then 127.0.0.1: refused, the
local server never hit. Pinning: the request reaches the judged address with
the original `Host`. TLS: the request carries `sni_hostname`. Redirect to an
internal address: refused. 21 address classes parametrized.
**Limits.** Behind an HTTP(S) proxy the proxy resolves names; coded then checks
its own answer (old behaviour). Fail-closed was not chosen because corporate
users depend on proxies; documented in README.

### SEC-02 — Private storage (MEDIUM, fixed on POSIX)
**What was wrong.** Session JSON (whole conversations), mission state/reports
and `trusted.json` were written with the default umask (typically 0644 in a
0755 folder). `SessionStore.load(id)` joined an unchecked id into a path.
**Exploitable.** On multi-user machines by any local account; the traversal
needed a crafted `--resume` argument (local).
**Change.** `securefs.private_dir` (0700, symlink refused, existing folder and
its files tightened once) and `write_private` (exclusive `O_EXCL|O_NOFOLLOW`
temp file created 0600 before any byte, fsync, atomic rename; writers of
one path take turns in-process, and a rename Windows refuses because the
target is open elsewhere is retried briefly). `check_id`
rejects anything but `[A-Za-z0-9._-]` names. Format unchanged; old sessions
load.
**Limits.** Windows ignores POSIX modes; the default locations are inside the
user profile, whose ACL limits access. Keep `CODED_SESSIONS_DIR` there.

### SEC-05 — Browser (MEDIUM, fixed / narrowed)
**What was wrong.** `page.route` covered the first page only; `window.open`
pop-ups and service workers were not checked; downloads were accepted.
**Change.** `context.route` for every page, `service_workers="block"`,
`accept_downloads=False`, pop-ups closed and listed in the result.
**Verified.** Real headless Chromium: a page doing `window.open`, `fetch` and
an iframe to 127.0.0.1 — the local server receives nothing; blocked URLs are
reported.
**Limits.** Chromium resolves names itself, so a rebinding window remains
between the check and Chromium's connection (narrowed by checking every
request). WebSocket traffic is not covered by Playwright routing in all
versions. Use `web_fetch` for untrusted URLs where possible.

### SEC-06 — Session concurrency (MEDIUM, fixed)
See `EXECUTION_LIFECYCLE.md`. Verified: two agents run concurrently; a second
`run` on a busy agent raises `AgentBusy` and never enters the history; the
lock is released after an exception and a KeyboardInterrupt; a live process
holding a session blocks `--resume`/`/resume` (pid shown) and a dead one's lock
is taken over; a lock from another host is respected; 6 threads × 25
concurrent saves leave valid JSON and no temp files; a running mission cannot
be resumed twice.
**Limits.** Liveness across hosts on a shared folder cannot be checked: such
a lock must be removed by hand (`.<id>.lock` beside the session).

### SEC-08 — Secret redaction (MEDIUM, fixed)
`redact.py` masks provider token formats (OpenAI/Anthropic, GitHub, GitLab,
Slack, AWS, Google, Hugging Face), `Authorization`/Bearer/Basic values, URL
and DSN passwords, `password=`-style assignments (not `max_tokens=4096`), PEM
private keys, JWTs, and the values of this process's sensitive environment
variables. Applied to the event log, telemetry spans, `/export`, mission
reports (therefore also `--notify-url` posts) and `explain_failure`.
Deliberately **not** applied to the saved session (it must stay an exact
record; it is private instead) or to the user's files.
**Limits.** Pattern-based: an unusual secret format with an innocuous name can
pass.

### SEC-07, SEC-09, SEC-10
- **SEC-07** fixed with a `finally`; the test fails on the old code.
- **SEC-09**: required fields, field types (forgiving `"5"`, `"true"`), enums,
  `additionalProperties: false`, an 8 MiB cap per call; refusals tell the model
  how to retry and are logged (`invalid_args`). Nested shapes are left to the
  tools (several accept loose forms on purpose).
- **SEC-10**: the existing outside-project check (realpath) already stops
  symlink and `../` escapes, `textio` refuses non-UTF-8 edits, `edit` fails
  cleanly on a stale `old_string`, `.codedignore` blocks read and write, and
  `read` is bounded. Tests added; no code change.

## Test results (this run)

- Linux, Python 3.13, with bubblewrap and Playwright/Chromium installed:
  **863 passed, 10 skipped** — the 10 need a Windows shell.
  (Baseline before the work: 785 passed, 14 skipped.)
- Package: `pip wheel .` builds `coded-0.5.0-py3-none-any.whl`, which contains
  the new modules; `import coded.cli, coded.agent` does not import `openai`.
- macOS and Windows: not run here. CI (`.github/workflows/tests.yml`) runs the
  suite on ubuntu/macos/windows; the new code avoids POSIX-only calls on
  Windows (`O_NOFOLLOW` optional, no `getuid`, process liveness via
  `OpenProcess` instead of `os.kill(pid, 0)`, which would terminate the
  process), and POSIX-mode and symlink tests are skipped there.
- No linter or type checker is configured in the repository.

## Remaining known limitations

1. `web_fetch` behind an HTTP(S) proxy: local resolution is checked, the proxy
   resolves independently.
2. Browser: Chromium's own DNS resolution leaves a narrowed rebinding window;
   WebSockets may bypass routing.
3. Windows: no sandbox backend; private files rely on the profile ACL.
4. Redaction is pattern-based and applies to outgoing text, not the session.
5. Session locks on shared network folders across machines need manual
   cleanup if a machine dies.
6. `bash` is not covered by `.codedignore` and its file changes are not
   checkpointed (unchanged, documented behaviour).
