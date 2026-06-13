---
description: Show ToolForge stats: total installs, top 5 rated tools, last 5 ratings, session tool-call count, integrity verification, and health warnings for installed tools.
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

## Step 4: Integrity

Shell out to:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_lockfile.py" verify --all --json
```

Parse the JSON `results` array and print one line per tool under an `Integrity:` heading:

- `status == "ok"` → `<tool_name>: verified`
- `status == "modified"` → `<tool_name>: MODIFIED: <n> file(s) changed` where `<n>` = `len(changed) + len(new)`. List the `changed`/`new` rel paths indented below.
- `status == "missing"` → `<tool_name>: missing (<rel paths from the "missing" array>)`

Tools installed under `~/.claude/skills/` or `~/.claude/plugins/` that do NOT appear in `results` have no lockfile rows — list each as `<tool_name>: unpinned`.

Exit codes: 0 = all verified, 2 = any modified/new files, 3 = any missing files. If the exit code is non-zero, add this warning:

> "⚠ One or more tools changed on disk after their install-time security scan. If YOU edited the tool, re-pin it as legitimate with:
> `python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_lockfile.py" pin <tool_name> ~/.claude/skills/<tool_name>`
> If you did NOT edit it, treat this as possible tampering — do not use the tool until you have reviewed the changed files."

If the script itself fails to run (exit 1, missing Python, etc.), print `Integrity: check unavailable` and continue — never block the rest of the dashboard on this step.
