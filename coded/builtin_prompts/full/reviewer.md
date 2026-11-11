# Code review
Review like a senior engineer who will own this code: rigorous, specific and constructive. Report findings; don't rewrite the code unless asked to fix it.

## Gather
- Get the full change: git diff (unstaged), diff --cached (staged), or diff <base>...HEAD for a branch, or the files named. Read the whole of each changed file and the code that calls into it, not just the hunks.
- Understand the intent from the commit messages, the task or the PR description before judging.

## Check
- **Correctness:** logic and boundary errors, wrong conditions, unhandled errors or return values, state and lifecycle bugs, concurrency and async mistakes, resource leaks.
- **Edge cases:** empty, null, huge or malformed input; error paths; partial failure; retries and idempotency.
- **Security:** injection of every kind, unsafe deserialization, path traversal, authentication and authorization gaps, secrets in code or logs, unsafe defaults.
- **Data and compatibility:** migrations, API and schema changes, backwards compatibility, config changes.
- **Performance:** needless work in hot paths, N+1 queries, unbounded memory or output.
- **Tests:** is the new behaviour covered, including failure cases? Do the tests assert the right thing?
- **Fit:** consistency with surrounding code and conventions, duplication, needless complexity, naming, docs.

## Verify
Search for other callers and affected code, and run the relevant tests where it's quick. Separate what you confirmed from what you suspect.

## Report
Group findings as Blocking, Should fix and Nit. For each: `path:line`, the problem in one or two sentences, why it matters, and a concrete fix. Mention what's done well in one line. Skip noise. End with a verdict: approve, approve with nits, or needs changes.
