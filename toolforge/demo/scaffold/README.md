# ToolForge Demo Scaffold

## What this is

A minimal Vite + React 18 app used for the ToolForge stage demo. Two terminals run the same prompt ("Build me a pricing card component."). The left terminal is vanilla Claude Code; the right has ToolForge installed plus shadcn-mcp and a UI component MCP wired in via `/toolforge`. Both edit `src/PricingCard.jsx` live.

## First-time setup

Windows:

```powershell
./setup.ps1
```

macOS / Linux:

```bash
./setup.sh
```

Requires Node.js 18+ and npm on PATH.

## Run the demo

```bash
npm run dev
```

Open http://localhost:5173. The page starts blank (the stub component renders `null`). As the agent edits `src/PricingCard.jsx`, Vite hot-reloads the result.

## Reset between rehearsals

Windows:

```powershell
./reset.ps1            # prompts before touching the DB
./reset.ps1 -Force     # no prompt
```

macOS / Linux:

```bash
./reset.sh             # prompts before touching the DB
./reset.sh -y          # no prompt
```

The reset script backs up `~/.claude/toolforge.db` to `~/.claude/toolforge.db.bak-<timestamp>` before clearing it, then restores `src/PricingCard.jsx` to the empty stub from `src/PricingCard.template.jsx`.

## If the live agent fails on stage

Copy the static reference card over the stage file:

```bash
cp src/PricingCard.reference.jsx src/PricingCard.jsx
```

Vite reloads and the audience sees a finished 3-tier pricing card. Carry on with the narration.
