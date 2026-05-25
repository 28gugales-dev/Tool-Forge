---
description: Show ToolForge stats: total installs, top 5 rated tools, last 5 ratings, current session tool-call count.
---

You are running the `/toolforge-status` command. Print a compact dashboard.

Shell out to:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_db.py" status
```

Then print the stdout to the user verbatim. The script formats the dashboard in plain ASCII so it reads cleanly in the terminal.

If the database does not exist yet, print: "No ToolForge activity yet. Run /toolforge <category> to start."
