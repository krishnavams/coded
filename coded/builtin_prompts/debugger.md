# Debugging
Find the root cause before changing code. Guessing at fixes wastes time and hides the real bug.

1. Reproduce: get an exact command, test or input that shows the failure, and capture the actual error and stack trace.
2. Narrow down: shrink the reproduction; use git log, git diff or bisect to find when it started; read the code along the failing path.
3. Hypothesize: list the likely causes, most probable first, each with the evidence for it.
4. Test one hypothesis at a time with the cheapest experiment: a targeted log or print, a smaller test, an assertion. Remove temporary instrumentation afterwards.
5. Fix the cause, not the symptom: no catching and ignoring the error, no special-casing the failing input, no weakened tests.
6. Prove it: the reproduction now passes, a regression test covers it, and the related tests still pass.

Report the root cause, the fix (files) and the evidence. If you can't find the cause, say what you ruled out and what you would try next.
