---
description: Decompose a complex multi-step task into an ordered skill pipeline, show the plan, get approval, execute each step with context flowing between them, and save the chain for future reuse.
argument-hint: <task with multiple phases>
---

You are running the `/forge` command. The user's task is: **$ARGUMENTS**

Invoke the `forge` skill with the full task: `$ARGUMENTS`

The forge skill will:

1. **Decompose** — break the task into 2–6 ordered sub-tasks based on action verbs, sequential connectors ("then", "after that"), and implicit phases in compound goals
2. **Route** — call `bin/toolforge_pipeline.py suggest-skill` for each sub-task in parallel to find the best installed skill or fall back to built-in Claude capabilities
3. **Check history** — look up whether this skill chain has been run before and offer the saved template
4. **Plan** — render a plain-ASCII flowchart of the pipeline and ask for approval before touching any code
5. **Install** — if any required skills are missing, run security review + install with a single permission prompt
6. **Execute** — run each step in sequence, passing a context summary from each step to the next
7. **Save** — persist the successful pipeline to SQLite so it surfaces as a suggestion next time

If `$ARGUMENTS` is empty:
> "Usage: `/forge <multi-phase task>`
> Example: `/forge build a user auth system with a postgres schema, JWT endpoints, playwright E2E tests, and a security review`"

If the task resolves to only one step, suggest `/toolforge-hunt` instead:
> "This looks like a single-step task. Try `/toolforge-hunt $ARGUMENTS` to find the best tool for it."
