---
description: Rewrite the worst Bayesian-rated local skill with a SQLite-versioned frontier update-or-discard safety net. Run without arguments to start an improve pass, or pass "verdict <skill>" / "rollback <skill>" / "lineage <skill>" to settle or inspect a rewrite.
argument-hint: [verdict <skill> | rollback <skill> | lineage <skill>]
---

You are running the `/toolforge-improve` command. The user passed: **$ARGUMENTS**.

## The frontier model (read this first)

Every skill has a **frontier**: the single best generation kept so far, with its frozen score. Generation 0 is the original (seeded on the first-ever commit). Each rewrite is a candidate forked from the current frontier (`parent_generation`). At verdict time the candidate must **beat the frontier score**, not merely the commit-time baseline — the bar is the reigning champion, so improvements compound instead of drifting sideways:

- **PROMOTE** — new score > frontier score. The candidate becomes the new frontier; the next rewrite forks from it. Outcome `improved`.
- **DISCARD** — new score does not clear the frontier. SKILL.md is auto-reverted to the frontier content and the proposal is recorded `discarded` so it is never re-proposed. The frontier is left untouched.

This is the EvoSkill update-or-discard gate: only strictly-better candidates advance the lineage; everything else is rolled back automatically.

## Step 0: Parse the argument

- `/toolforge-improve` — full improve pass (Steps 1-7)
- `/toolforge-improve verdict <skill>` — jump to Step 8
- `/toolforge-improve rollback <skill>` — jump to Step 9
- `/toolforge-improve lineage <skill>` — jump to Step 10

Skill names must match `^[a-z0-9._@/-]{1,80}$`. If an argument doesn't, tell the user and stop. Do NOT pass arbitrary strings through to the shell — always pass the skill name as a separate, fully-quoted positional argument. The Python layer re-validates; quoting here is defense in depth, not the security boundary.

## Step 1: Find candidates

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_improve.py" candidates --json
```

Exit 1 / empty list means nothing qualifies (no skill with >= 3 ratings scoring below 2.8). Tell the user their skills are healthy, suggest `/toolforge-rate` to keep ratings flowing, and stop.

## Step 2: User picks the target

Render the candidates worst-first (score, rating count, path) and ask which skill to improve. Recommend the worst-rated one but let the user choose. Wait for the answer.

## Step 3: Package the skill for rewriting

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_improve.py" package "<skill>" --json
```

The bundle contains:
- `content` — current SKILL.md (head/tail-truncated if oversized)
- `rating_history` + `shrunk_score` — why this skill ranks where it does
- `prior_attempts` — the outcome ledger: every previous rewrite with its proposal and outcome (`improved`, `rolled_back`, `discarded`, `kept`, `pending`)
- `frontier_score` + `frontier_generation` — the bar this rewrite must clear (`null` if the skill has never been committed, i.e. no frontier yet)
- `discarded_generations` — how many prior generations already failed to beat the frontier; a high count means easy wins are exhausted and the rewrite must be genuinely better, not just different

If `prior_attempts` contains a `pending` entry, a previous rewrite is still unsettled — tell the user to run `/toolforge-improve verdict <skill>` (or `rollback`) first, then stop.

## Step 4: Draft the rewrite

Now act as the proposer. Analyze the bundle and draft a full replacement SKILL.md:

- Diagnose WHY the skill rates poorly: vague trigger description, missing examples, wrong scope, stale instructions.
- **You must clear the frontier, not just the old baseline.** If `frontier_score` is set, the rewrite has to score *above* it after fresh ratings or it is auto-discarded — aim higher than "slightly different".
- **Never re-propose an idea from a `discarded` or `rolled_back` prior attempt.** Cite the ledger explicitly: "iteration 2 tried X and was discarded, so this rewrite instead does Y."
- Keep the YAML frontmatter valid (`name` must still match the directory name); preserve what works, fix what doesn't. Prefer editing over wholesale replacement.
- Write a one-sentence proposal summary — it goes into the ledger so future passes know what this attempt tried.

## Step 5: Show the diff and get EXPLICIT approval

Show the user a concise before/after diff of the meaningful changes plus your proposal summary. Then ask:

> "Apply this rewrite to `<skill>`? The original is snapshotted into SQLite first, so `/toolforge-improve rollback <skill>` restores it at any time. [y/N]"

**Do NOT write anything until the user explicitly approves.** Anything other than a clear yes = stop, no files touched.

## Step 6: Commit atomically

Write the approved draft to a temp file (e.g. under the OS temp dir), then:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_improve.py" commit "<skill>" "<temp_draft_path>" --proposal "<one-sentence summary>"
```

This snapshots the full original into the `skill_versions` table (with the current shrunk score as baseline), then atomically replaces SKILL.md (tmp file + `os.replace`). On the **first-ever commit** for a skill it also seeds the frontier at generation 0 (the original content, frozen baseline score); every commit records `parent_generation` = the frontier generation it was forked from. Report the returned `version_id`, `baseline_score`, and `frontier_score` (the bar the rewrite must clear; `null` on the first commit). Delete the temp draft.

## Step 7: Remind about the rating loop

> "Rewrite applied. Rate this skill with `/toolforge-rate <1-5>` as you use it — after 3 new ratings, run `/toolforge-improve verdict <skill>` to promote or revert."

## Step 8: Verdict mode — the frontier gate

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_improve.py" verdict "<skill>"
```

The gate compares the new shrunk score against the **frontier score** (the best generation kept so far), not the commit-time baseline. The JSON includes `frontier_score`, `new_score`, `generation`, and `parent_generation`.

- `PENDING` — fewer than 3 post-rewrite ratings; tell the user how many more are needed (`new_ratings` / `needed`).
- `PROMOTE` — `new_score` beat the frontier. The version is marked `improved`, the frontier **advances** to this generation, and `eval_score` is recorded. The next improve pass will fork from here. Celebrate briefly.
- `DISCARD` — `new_score` did not clear the frontier. The command has **already auto-reverted** SKILL.md to the frontier content (no rollback step needed) and marked the proposal `discarded` so it is never re-proposed. The frontier is unchanged. Report `reverted_to_generation` to the user — the skill is back on its best-known version.

DISCARD is automatic and final for that candidate; you do not run rollback after a DISCARD.

## Step 9: Rollback mode

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_improve.py" rollback "<skill>"
```

Manually restores the snapshotted backup of a still-`pending` rewrite and marks it `rolled_back` — use this to abandon a rewrite *before* it reaches a verdict (e.g. the user changes their mind). After a DISCARD verdict there is nothing to roll back; the revert already happened. The ledger keeps the failed proposal so it is never re-proposed. Confirm what was restored and from which version.

## Step 10: Lineage mode

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_improve.py" lineage "<skill>" [--json]
```

Prints the full version chain — each generation with its `parent_generation`, `outcome`, `baseline_score`, `eval_score`, and `created_at` — plus the current frontier marker (`*FRONTIER*` on the active generation). Without `--json` it renders an ASCII tree (oldest generation first) matching the `/toolforge-status` box style; with `--json` it returns `{skill, frontier, versions[]}` for programmatic use. Use it to show the user how a skill evolved: which rewrites were promoted, which were discarded, and what the reigning version is. Exit 1 if the skill has no recorded versions yet.
