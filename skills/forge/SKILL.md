---
name: forge
description: Decompose a complex multi-step task into an ordered pipeline of the best installed skills and tools, show the plan as a visual flowchart, get user approval, execute each step in sequence with context flowing between steps, then save the chain for future reuse. Activate on `/forge <task>` or when the user describes a task with multiple distinct phases (design + implement + test + review, or any chain of 2+ different capabilities). Do NOT activate for single-step tasks, simple questions, or tasks already handled by a single skill — use toolforge-hunter for those.
license: MIT
---

# Forge — Skill Pipeline Orchestrator

You receive a multi-phase task in `$ARGUMENTS`. Your job: break it into steps, assign the best installed skill to each step, show the user the plan, get approval, execute the pipeline, then remember the chain.

**Rule**: Never start actual work before Phase 4 (user approval). Decompose and plan first.

---

## Phase 1 — Parse and Decompose

Read `$ARGUMENTS` carefully.

**Identify sub-tasks** by scanning for:
- Sequential connectors: "then", "after that", "next", "finally", "once X is done"
- Parallel action verbs appearing separately: design, build, implement, write, test, review, deploy, migrate, document, generate, analyze, refactor, lint, format, seed, scrape, index
- Enumerated phases: "1. ... 2. ... 3. ..." or "first ... second ... third ..."
- Implicit phases in compound goals: "full-stack feature" implies schema + implementation + tests

**Produce a numbered list of sub-tasks**, each as a single imperative sentence:
- "Design the database schema for X"
- "Implement the API endpoint for Y"
- "Write E2E tests covering Z"
- "Run a security review of the implementation"

**Constraints**:
- Minimum 2 sub-tasks (if only 1, use `/toolforge-hunt` instead)
- Maximum 6 sub-tasks (if more, consolidate by grouping closely related actions into one step)
- Each sub-task must be independently actionable

If `$ARGUMENTS` is empty or cannot be decomposed into 2+ sub-tasks:
> "forge needs a multi-phase task. Example: `/forge build auth feature with postgres schema, JWT endpoints, playwright tests, and a security review`"

Then stop.

---

## Phase 2 — Route Each Sub-Task to a Skill

For each sub-task, call the routing engine:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_pipeline.py" suggest-skill "<sub-task description>"
```

This returns a JSON array of `[{skill_name, skill_type, score, description}, ...]`.

**Rules for skill assignment**:
- If the top result has `score >= 0.05` and `skill_type != "builtin"`: assign that skill to the step
- If the top result is `(built-in)` or score < 0.05: the step runs as standard Claude work (no skill needed)
- If a skill appears in multiple steps: that is fine — list it at each step that uses it
- If the same sub-task matches several skills with close scores (within 0.05): note both in the step as options

Run all `suggest-skill` calls in parallel — one Bash call per sub-task in a single tool-use block.

Build the steps array. Each step object:

```json
{
  "step": <N>,
  "skill_name": "<name or (built-in)>",
  "skill_type": "<skill|mcp|plugin|builtin>",
  "sub_task": "<imperative sentence>",
  "score": <float>,
  "description": "<skill description or empty>"
}
```

---

## Phase 3 — Check for Past Pipelines

Compute the steps hash:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_pipeline.py" hash '<steps_json>'
```

Check if we have run this skill chain before:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_pipeline.py" similar '<steps_hash>' 3
```

If any past pipeline is returned AND its `success` is `true`:
> "I've run this pipeline before (last run: <run_at>): <task_desc>. Use the same chain? [Y/n]"
>
> If user says Y: skip to Phase 4 with the saved chain.
> If user says N: continue with freshly routed chain.

---

## Phase 4 — Render the Plan and Get Approval

Shell out to render the ASCII flowchart:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_pipeline.py" render "<task_desc>" '<steps_json>'
```

Print the rendered output verbatim. It looks like:

```
============================================================
forge: "build auth feature with postgres schema and tests"
============================================================

  1. sql-schema           Design the postgres users/sessions schema  [0.72]
                        |
  2. (built-in)           Implement the JWT auth endpoints
                        |
  3. playwright-testing   Write E2E login and logout tests  [0.61]
                        |
  4. code-review          Security review of the implementation  [0.58]

  3 skill(s)  |  1 built-in step(s)

  Proceed? [Y/n]  Skip a step? (type 'skip N')  Edit? (type step number)
```

**Wait for the user's response.**

Accepted responses:
- `Y` / `y` / `yes` / Enter → proceed with full pipeline
- `N` / `n` / `no` → abort; ask "Want to describe the task differently?"
- `skip 2` / `skip 2,3` → mark those steps as skipped; continue with the rest
- `<number>` → user wants to edit that step; ask "What skill or description for step N?" then update and re-render

---

## Phase 5 — Install Missing Skills (if any)

Before execution, check each non-builtin step:

```
claude plugin list
claude mcp list
```

and check if `~/.claude/skills/<skill_name>/SKILL.md` exists.

For any skill in the plan that is **not yet installed**:
1. Tell the user: "Step N requires `<skill_name>` which is not installed."
2. Run the security review handoff for web-discovered tools:
   ```
   PROMPT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_security_handoff.py" prompt "<source_url_if_known>" "<skill_name>")
   ```
   (If source_url unknown: skip the security handoff and proceed — the skill was router-matched from local/installed inventory, which means it IS installed or it's a known catalog entry)
3. Install:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_install.py" "<skill_name>" "<install_command>" "<category>" --yes
   ```

Batch all installs in one permission prompt if multiple skills are missing (use `--batch` mode per the installer spec).

If any install fails with a `malicious` security verdict: remove that step from the pipeline and announce it.

---

## Phase 6 — Execute the Pipeline

Announce: `"Starting forge pipeline: <N> steps"`

For each step (in order, skipping any marked 'skip'):

### Step announcement
```
─── Step N/total: <skill_name> ───────────────────────────
<sub_task>
```

### Context injection
Each step receives the output of the previous step as context. Build a context block:

```
[Context from previous step: <skill_name>]
<previous step's output summary — max 500 words>
```

Pass this context along with the sub_task description when invoking the skill or performing the built-in work.

### Skill invocation
- If `skill_type == "skill"`: invoke via the Skill tool with input `{"skill": "<skill_name>"}` and the sub-task + context as the prompt
- If `skill_type == "mcp"`: use the MCP server's relevant tool directly
- If `skill_type == "builtin"`: Claude performs the work directly using standard capabilities

### Step completion
After each step completes:
```
  Step N done. ✓
```
Capture a 1–3 sentence summary of the step's output to pass as context to the next step.

### Error handling
If a step fails (skill throws, tool errors out, user says "this is wrong"):
1. Print: `"Step N encountered an issue: <brief error>"`
2. Ask: "Retry this step? [Y/n] | Skip and continue? (s) | Abort pipeline? (a)"
3. Act on the response.

---

## Phase 7 — Save and Summarize

After the pipeline completes (all non-skipped steps done):

**Save the pipeline to the learning store:**

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_pipeline.py" save "<task_desc>" "<steps_hash>" '<steps_json>' --success
```

**Print the completion summary:**

```
============================================================
forge complete: <N_done>/<N_total> steps
Skills used:    <comma-separated skill names>
Pipeline saved: chain will be suggested on similar tasks
============================================================

Run /toolforge-rate <1-5> to rate the skills used.
```

If any steps were skipped: list them with "(skipped)" annotation.

If the pipeline was aborted mid-way (user chose abort in Phase 6 error handling): save it WITHOUT `--success` and print: `"Pipeline saved (incomplete) for debugging."`

---

## Constraints and Rules

**Always decompose before working.** Never start code/implementation before Phase 4 approval. The plan is the product of Phases 1–4; the execution is Phases 5–6.

**Skill routing is advisory.** If a suggested skill seems wrong for the sub-task (you have domain knowledge that it's a poor match), note it in the step description and mark it as `(built-in)` instead. Trust your judgment over the score when the mismatch is obvious.

**Context window discipline.** Pass only a 1–3 sentence summary of each step's output to the next step, not the full output. The forge session is already using context for pipeline management; don't blow the window passing 5,000 tokens of step output forward.

**No invention.** Never fabricate install commands, skill names, or source URLs. If suggest-skill returns nothing useful, use `(built-in)`.

**One permission prompt per install.** If multiple skills need installing, batch them. Don't ask the user to approve each separately.

**The pipeline is the memory.** After completion, the saved pipeline will be suggested the next time someone asks `/forge` for a similar task. Write `sub_task` descriptions clearly enough that they remain useful as a template for future runs.
