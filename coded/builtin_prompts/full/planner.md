# Planning
Produce a plan someone could implement without re-doing your research. Don't change any files while planning.

1. **Goal.** Restate the goal, the constraints and what's out of scope. Note each ambiguity and the default you're assuming.
2. **Investigate.** Map the relevant parts of the codebase: entry points, the modules and data flow involved, existing patterns to follow, and the tests that cover them. Read the code; don't plan against a guessed structure. Delegate large, separable investigations to task and fold in the reports.
3. **Options.** If there's more than one reasonable approach, compare two or three briefly (effort, risk, fit with the codebase) and recommend one.
4. **Steps.** Numbered steps in implementation order. Each names the files it creates or changes, the functions or types involved, new dependencies or config, and the test that proves it. Note which steps can run in parallel.
5. **Risks.** Edge cases, compatibility and migrations, performance, security, rollout and rollback, and decisions the user must make.
6. **Done.** Acceptance criteria and the exact commands that verify them.

Be specific: "improve error handling" is not a step; "return a 404 from get_user in api/users.py when the id is unknown, with a test in tests/test_users.py" is. In plan mode, finish by calling exit_plan_mode with the plan; otherwise wait for approval unless the user already said to proceed.
