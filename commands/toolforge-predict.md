---
description: Run the ToolForge predictive engine to surface which skills you are most likely to need in this session, based on your history, pipeline patterns, and the current prompt.
---

You are running the `/toolforge-predict` command.

## Step 1: Get the session ID

Use the environment variable `CLAUDE_SESSION_ID` if set. Otherwise use a fallback of `session-unknown`.

```
SESSION_ID="${CLAUDE_SESSION_ID:-session-unknown}"
```

## Step 2: Run the predictor

Shell out to:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_predictor.py" predict_and_log "$SESSION_ID" "<user's current prompt if available>"
```

If no prompt text is available, omit it:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_predictor.py" predict_and_log "$SESSION_ID"
```

The predictor will output a human-readable prediction block followed by a JSON array. Print the human-readable block verbatim:

```
[ToolForge predictor] Skills likely needed this session:
  1. playwright           ██████████ (87%)
  2. context7             ████████   (72%)
  3. sequential-thinking  █████      (51%)
```

## Step 3: Offer to install missing predictions

Parse the JSON array from the predictor output (the last line). For each predicted skill, check if it is already installed by running:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_local_scan.py" check <skill_name>
```

If any predicted skills are not installed, print:

> "Some predicted skills are not installed. Run /toolforge-hunt <skill_name> to install them, or /toolforge <category> to browse alternatives."

List the missing ones by name.

## Step 4: Show prediction accuracy

Shell out to:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_predictor.py" accuracy
```

Print the accuracy report only if total predictions > 10 (to avoid showing meaningless stats early on). Otherwise skip this step.

## Step 5: Confirm usage at session end

At the end of this session, call confirm for each skill that was actually invoked. This is handled automatically by the `session-start-predictor` hook — no manual action needed.
