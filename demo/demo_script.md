# ToolForge side by side demo

Two terminal windows, same machine, same prompt repo. Left is vanilla Claude Code. Right has ToolForge installed and the demo flow exercised.

## Setup

1. First time on a machine: run the tracked scaffold installer.

   - Windows: `toolforge/demo/scaffold/setup.ps1`
   - POSIX: `toolforge/demo/scaffold/setup.sh`

   This installs node_modules for the React + Vite scaffold and resets `src/PricingCard.jsx` to the empty stub.

2. To start the demo server: `cd toolforge/demo/scaffold && npm run dev`, then open `http://localhost:5173`. Keep this tab visible on the projector alongside both terminals.

3. Open two terminals in the scaffold directory.

4. Left terminal: launch Claude Code with NO ToolForge plugin installed. Do not install the official `frontend-design` skill on this side. The visual gap is the point.

5. Right terminal: launch Claude Code with ToolForge installed:

   ```
   claude plugin install toolforge@local-toolforge
   ```

   Do not install any UI plugins or MCPs yet. The discovery moment is part of the show.

Full 30-minute-before-stage checklist lives in `toolforge/demo/pre_demo_checklist.md`. Walk it every time.

## Pre-flight verification (run immediately before going live)

1. `claude mcp list` on both terminals. Confirm no shadcn or magic MCP is already installed on either side.
2. `claude plugin list` on both. Confirm `toolforge` is present on the right, absent on the left.
3. `python toolforge/bin/toolforge_db.py status`. Confirm the DB is empty (or freshly reset).

If any check fails, run `toolforge/demo/demo_reset.ps1` (or `.sh`) and re-verify.

## Run order

Total target time: 4 minutes 30 seconds. Speaker callouts below are short. Full talking-point depth in `toolforge/demo/speaker_notes.md`.

### Step 1 (00:00 to 00:20) Frame the problem

> Speaker: "Anthropic Tool Search picks from tools you already installed. It does not help you find new ones. We do, live from the web, and we learn which ones actually help you."

### Step 2 (00:20 to 01:10) Run the same prompt on the left

In the LEFT terminal, type:

```
Build me a pricing card component in src/PricingCard.jsx.
```

Let Claude generate. Expect a basic Tailwind or unstyled card. Acceptable. Do not interrupt.

> Speaker: "Stock Claude Code. No special tools. Reasonable result, but generic."

### Step 3 (01:10 to 01:30) Switch focus to the right

> Speaker: "Now watch what ToolForge does first."

In the RIGHT terminal, type:

```
/toolforge UI
```

Expected: live web search runs, top 5 list appears with `shadcn-ui-mcp` at position 1 or 2.

### Step 4 (01:30 to 02:00) Approve installs

The curator prompts ONE pick per invocation. You will run `/toolforge UI` twice in this step:

1. First pick: `shadcn-ui-mcp` (display name), backed by package `@jpisnice/shadcn-ui-mcp-server`. Confirming with `y` runs this exact command on stage:

   ```
   claude mcp add shadcn-ui -- npx -y @jpisnice/shadcn-ui-mcp-server
   ```

2. After the first install completes, RE-RUN `/toolforge UI` and pick the second one: `magic-mcp` (display name), backed by `@magicuidesign/mcp` (server registered as `magic`, matching `fallback/ui.json`):

   ```
   claude mcp add magic -- npx -y @magicuidesign/mcp
   ```

Pick the MCP variant (not the `frontend-design` plugin) so the audience sees real MCP install + restart text on stage. Confirm both with `y`. Every shell call routes through the ToolForge argv allow-list (visible security boundary). The second `/toolforge UI` also shows the ranking already shifted because `shadcn-ui-mcp` just moved into the `installs` table.

> Speaker: "Two real MCP servers, installed live, through a sandboxed allow-list."

### Step 5 (02:00 to 03:30) Same prompt on the right

In the RIGHT terminal, type the same prompt:

```
Build me a pricing card component in src/PricingCard.jsx.
```

Expected: Claude pulls real shadcn primitives via the MCP, applies the magic-ui MCP, and produces a polished, animated, well-typeset card. Visual contrast with the left side should be obvious.

Note: newly installed MCP servers may require a Claude Code restart to register. Presenter choice:

- Path A (cleaner): pre-install both MCPs cold before the talk, run `/toolforge UI` purely for the visual ranking demo, do not re-install on stage.
- Path B (more dramatic): install live in Step 4, restart Claude Code between Step 4 and Step 5, eat the 10-second pause and narrate it.

Decide before stage. Mark the chosen path on the rehearsal log.

> Speaker: "Same prompt. Same model. Different tool surface. That is the whole pitch."

### Step 6 (03:30 to 04:00) Rate the result

Right terminal:

```
/toolforge-rate 5
```

Expected: "Rated shadcn-ui-mcp = 5/5. New avg: 5.00."

The rating writes to `~/.claude/toolforge.db` and persists across sessions. Verify mid-rehearsal with:

```
python toolforge/bin/toolforge_db.py status
```

> Speaker: "One number. Stored locally. Feeds the next ranking."

### Step 7 (04:00 to 04:30) Show the learning loop

Right terminal:

```
/toolforge-status
```

Expected: dashboard with `shadcn-ui-mcp` at position 1 in Top 5 rated tools, total installs of 2, the 5/5 rating in the last 5 ratings table.

Then re-run:

```
/toolforge UI
```

Expected: `shadcn-ui-mcp` ranks higher than the first time because the Likert weight kicked in.

> Speaker: "Watch the rating change the ranking. That is self-learning visible in 10 seconds."

## Failure recovery

Full failure decision tree in `toolforge/demo/failure_recovery_tree.md`.

## Backup video

OBS configuration in `toolforge/demo/obs_config.md`.

## Practice checklist

Track every dry run in `toolforge/demo/rehearsal_log.md` (10-row log: 10 end-to-end runs, 1 with network unplugged, 1 on a fresh machine, all confirmed under 4 min 30 s). Offline rehearsal procedure in `toolforge/demo/network_off_rehearsal.md`.

## Reset between rehearsals

`toolforge/demo/demo_reset.ps1` (Windows) or `demo_reset.sh` (POSIX). Backs up `~/.claude/toolforge.db` to a timestamped file, deletes the live DB, restores `PricingCard.jsx` to the empty stub. Honors `-Force` (PowerShell) and `--force` (POSIX) to skip the confirmation prompt.
