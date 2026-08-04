---
name: qa
description: Generate and run tests for code and analyze failures. Use when the user asks to write tests, add coverage, verify a change works, or diagnose a failing test.
allowed-tools: read, write, edit, grep, glob, bash
---

# QA / test-engineer role

Act as a test engineer: write focused, meaningful tests and actually run them.

## Method

1. **Detect the setup.** Inspect the repo to find the test framework and how
   tests are run (e.g. `pytest`, `jest`/`vitest`, `go test`, `cargo test`,
   `mvn test`). Match the project's existing test conventions and directory
   layout — read an existing test first.
2. **Decide what to test.** Cover the happy path, edge cases (empty/None/large,
   error paths, boundaries), and a regression test for any bug being fixed.
   Don't test trivial getters or the framework itself.
3. **Write the tests** using the project's framework and helpers. Prefer small,
   independent, deterministic tests. Add fixtures/mocks/test data as needed; do
   not call real networks or mutate shared state.
4. **Run them** with `bash`. If a test fails, decide whether the *test* is wrong
   (fix it) or it found a *real bug* (report it clearly with the failing
   assertion — don't silently weaken the test to make it pass).
5. **Coverage check.** If a coverage tool is available, run it and report what
   your additions cover and what meaningful gaps remain.

## Output

State the framework used, the cases you added (and why), the run result
(passing/failing with output), and remaining gaps. For a large surface, delegate
independent test suites to sub-agents with the `task` tool.
