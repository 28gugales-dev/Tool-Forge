---
description: Launch the ToolForge Flow Studio in your browser. Drag-drop builder for skill/MCP/plugin chains; export flows as auto-triggered Claude skills.
allowed-tools: Bash
---

# /toolforge-ui

Start the local Flow Studio. Spawns a stdlib HTTP server (no pip installs, no npm, no permission prompts beyond the one bash call below), opens the browser, returns.

Run:

```bash
python "${CLAUDE_PLUGIN_ROOT}/webui/launch.py"
```

The server runs at `http://127.0.0.1:7321` (auto-bumps the port if busy). It serves:

- **Inventory pane**: every installed skill, plugin, MCP server, agent, slash command, plus the 19 discovery repos from `~/.claude/CLAUDE.md`. Each tagged by type + inferred category. Ratings come from the existing `~/.claude/toolforge.db` (Bayesian-shrunk decayed score, same formula as `/toolforge-status`).
- **Canvas**: drag tools onto the grid, wire them top-to-bottom, click any node to add a per-step prompt annotation.
- **Inspector**: set the annotation, open the tool's source file, delete the node.
- **Export**: writes `~/.claude/skills/toolforge-<your-trigger>/SKILL.md` with MANDATORY trigger phrases. After Claude Code reloads skills, typing `/<your-trigger>` walks the chain.

After exporting a flow, restart Claude Code (or run `/init`) so the new skill is picked up.

To stop the server: kill the python process for `webui/server.py`, or close the terminal where it spawned. The launcher logs to `webui/server.log`.
