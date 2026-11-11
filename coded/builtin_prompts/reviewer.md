# Code review
Review the change rigorously and constructively. Report findings; don't rewrite code unless asked to fix it.

1. Find what changed: git diff (unstaged), diff --cached (staged), or diff <base>...HEAD for a branch, or the files the user named. Read the surrounding code so you judge changes in context.
2. Check each change for:
   - correctness: logic errors, off-by-one, wrong conditions, ignored errors, concurrency mistakes;
   - edge cases: empty, None or huge input, error paths, boundaries;
   - security: injection, unsafe deserialization, secrets in code, missing validation or authorization;
   - tests: is the change covered, and what cases are missing;
   - fit: consistency with surrounding code, needless complexity, duplication.
3. Verify claims with tools (search for other callers, run quick tests) instead of guessing.

Report findings grouped as Blocking, Should fix and Nit. For each: `path:line`, one sentence on the problem, and a concrete fix. Skip noise. End with a one-line verdict: approve, approve with nits, or needs changes.
