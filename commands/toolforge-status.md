---
description: Show ToolForge stats: total installs, top 5 rated tools, last 5 ratings, session tool-call count, and health warnings for installed tools.
---

You are running the `/toolforge-status` command. Print a compact dashboard.

## Step 1: Main stats

Shell out to:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_db.py" status
```

Print the stdout verbatim. If the database does not exist yet, print: "No ToolForge activity yet. Run /toolforge <category> to start." and stop.

## Step 2: Health report

Shell out to:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_update_checker.py" render
```

Print the stdout verbatim below the main stats. The checker uses a 6-hour cache — it will not do a live DB scan on every call.

Exit codes from the checker:
- 0 = all tools healthy
- 1 = one or more health flags found (stale, archived, inactive, low-rated, dormant)

If the checker exits 1, add this note:

> "Run /toolforge-hunt <task> to find better alternatives for flagged tools, or /toolforge <category> to browse fresh rankings."

## Step 3: Cache status

Print one line:

```
Router index: <fresh/stale> (cache at tempdir/toolforge_router_idx.json)
```

Determine fresh/stale by shelling out:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_router.py" route --check-cache
```

If the route command does not support --check-cache, skip this step silently.
