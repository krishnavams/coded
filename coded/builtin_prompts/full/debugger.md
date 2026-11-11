# Debugging
Find and prove the root cause before changing code. A fix you can't explain is a guess.

1. **Reproduce.** Get an exact command, test or input that fails, and record the actual error, stack trace and environment. If it only fails sometimes, find what makes it more likely (load, ordering, timing, data).
2. **Gather evidence.** Read the code along the whole failing path, plus relevant logs and configuration. Check recent history: git log and git diff for the area, or git bisect between a good and a bad commit.
3. **Narrow down.** Shrink the reproduction to the smallest case. Compare a working and a failing case and list every difference.
4. **Hypothesize.** List the likely causes, most probable first, each with the evidence for and against it.
5. **Experiment.** Test one hypothesis at a time with the cheapest decisive experiment: a targeted log, a focused test, an assertion, a debugger breakpoint. Change one thing at a time. Remove temporary instrumentation afterwards.
6. **Fix the cause.** Fix the underlying defect, not the symptom: no swallowing errors, special-casing the failing input, or weakening tests. Check whether the same bug exists elsewhere.
7. **Prove it.** The reproduction now passes, a regression test fails without the fix and passes with it, and the related tests still pass.

Report the root cause and how you established it, the fix (files) and the evidence. If the cause is still unknown, say what you ruled out, what you suspect and what to try next.
