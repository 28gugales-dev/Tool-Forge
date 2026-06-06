---
description: Clear the ToolForge local-source scan cache so the next /toolforge call re-scans installed plugins, installed MCPs, and configured local directories.
---

You are running the `/toolforge-rescan` command. The local-source scanner caches results per category for 5 minutes to keep `/toolforge` discovery within budget. This command forces a fresh scan on the next `/toolforge` invocation.

Shell out both commands (run them sequentially, print combined output):

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_local_scan.py" rescan-all
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_router.py" invalidate
```

Print both stdout outputs to the user verbatim.
Expected: `cleared <N> cache file(s)` then `Router index cache cleared.`

If the user wants to refresh a single category, tell them to pass `--force` to a `/toolforge` call (the curator skill propagates the flag to the scanner).

Local sources scanned (defaults):
- `~/.claude/skills`
- `~/.claude/agents`
- Project-local `.claude/skills` and `.claude/agents` if present in cwd
- `claude plugin list` and `claude mcp list` output

Additional local reference repos can be configured in `~/.claude/toolforge-config.json`:

```
{ "local_paths": ["/abs/path/to/skill-repo", "..."] }
```

Configured paths are scanned with a 4 KiB per-file read cap, depth cap of 4, file count cap of 2000, and an 8-second wall-clock budget. Symbolic links are not followed.
