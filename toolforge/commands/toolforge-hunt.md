---
description: Hunt for the best skill or tool for a specific task, install it, then immediately work on the task. Searches live for the highest-quality matching tool before touching the problem.
argument-hint: <task description>
---

You are running the `/toolforge-hunt` command. The user's task is: **$ARGUMENTS**

Invoke the `toolforge-hunter` skill with the full task description: `$ARGUMENTS`

The hunter will:
1. Parse the task to understand what capability and tech stack are needed
2. Search live for the best matching Claude Code skill, plugin, or MCP server
3. Score candidates by relevance to this specific task (not just by category)
4. Present the top 3 picks and ask which to install
5. Run a security review on any web-discovered pick before installing
6. Install the chosen tool
7. Immediately work on the task using the installed tool

If `$ARGUMENTS` is empty, remind the user:

> "Usage: `/toolforge-hunt <task description>`
> Example: `/toolforge-hunt animate a React hero section with GSAP scroll effects`"
