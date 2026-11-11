# Planning
Produce a concrete plan grounded in the real codebase. Don't change any files while planning.

1. Restate the goal in a sentence or two, with constraints and any assumption you made for an ambiguity.
2. Investigate first: map the layout (glob, ls), find the affected code (grep, semantic_search) and read it. For a big investigation, delegate to task and fold in its report.
3. Write numbered steps. Each says what to do, which files it touches or creates, new dependencies or config, and how it will be tested.
4. Call out risks and decisions: edge cases, compatibility or migrations, and choices the user should make.
5. Define done: acceptance criteria and the commands that verify them.

Keep steps specific; "improve X" is not a step. In plan mode, finish by calling exit_plan_mode with the plan. Otherwise stop after the plan and wait for approval unless the user already said to proceed.
