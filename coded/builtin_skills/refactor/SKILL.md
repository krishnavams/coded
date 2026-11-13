---
name: refactor
description: Find poor-quality code and improve it without changing behavior. Use when the user asks to refactor, clean up, simplify or reduce duplication, or runs /refactor.
---

# Refactoring role

Improve the structure of working code without changing what it does.

## Method

1. **Find the hot spots.** Run `repo_map` with `operation: "health"` (and the
   user's `path`, if any). It ranks long, branchy or deeply nested functions,
   long parameter lists and copy-pasted blocks, most-depended-on first.
2. **Pick a few, not all.** Choose the 1–3 findings with the best payoff for the
   least risk. Say which you picked and why; list the rest for later.
3. **Know the blast radius.** For each file you will change, run `repo_map`
   with `operation: "impact"` to see what depends on it and which tests cover it.
   If nothing tests the code, add a characterization test first that pins the
   current behavior.
4. **Refactor in small, behavior-preserving steps**: extract a function, return
   early instead of nesting, replace a long if/elif chain with a table, group
   related parameters, move a duplicated block into one shared helper. Keep
   public names and signatures unless the user agreed to change them.
5. **Check after each step.** Run the affected tests (or the project's checks).
   A failure means the step changed behavior: undo or fix it before going on.
   Never weaken or delete a test to make it pass.

## Output

What you changed (file, function, which smell, the technique), the tests you
ran and their result, and the remaining hot spots you left alone.
