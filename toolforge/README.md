# ToolForge

ToolForge is a Claude Code plugin that discovers the best tools (plugins, MCP servers, and skills) for the task you are about to do. It installs them with one keystroke through a curator skill that searches the live web behind a strict allow-list. It learns which ones actually helped you by collecting a 1 to 5 Likert rating after every session and re-ranking accordingly.

## Pitch

Open: "Anthropic Tool Search picks from the tools you already installed. It does not help you find new ones. We do, live from the web, and we learn which ones actually help you."

Close: "Watch the rating change the ranking."

## Architecture at a glance

```
+--------------------+        +-------------------------+
|   /toolforge UI    |  -->   |   toolforge-curator     |
|   (slash command)  |        |   (skill)               |
+--------------------+        +------------+------------+
                                           |
                       WebSearch + WebFetch | URL allow-list gate
                                           |
                          local-scan (~/.claude, project .claude,
                          configured local_paths, claude plugin/mcp list)
                                           v
                              +------------+------------+
                              |  SQLite-backed ranking  |
                              |  (stars, recency,       |
                              |   Bayesian Likert)      |
                              +------------+------------+
                                           |
                                  top 5    v
                              +------------+------------+
                              |  user picks one         |
                              +------------+------------+
                                           |
                              +------------+------------+
                              |  installer (argv        |
                              |  allow-list gate)       |
                              +------------+------------+
                                           |
                                           v
                              +-------------------------+
                              |   SQLite log + ratings  |
                              +-------------------------+
```

## Install

```
claude plugin marketplace add ./toolforge
claude plugin install toolforge@local-toolforge
```

That is it. The plugin auto-registers 4 slash commands, 1 skill, and 2 hooks. No external Python dependencies (stdlib only).

## What it looks like

Sample `/toolforge-status` output captured from a live session:

```
================ ToolForge Status ================
Total approved installs:  3
Current session tool calls:  unknown (no CLAUDE_SESSION_ID in env)

Top 5 rated tools (Bayesian-shrunk decayed score, matches curator ranking):
  shadcn-ui-mcp                   score 3.62  raw avg 4.67  (3 rating(s))
  magic-ui                        score 3.27  raw avg 3.50  (6 rating(s))
  frontend-design                 score 3.00  raw avg 3.00  (1 rating(s))

Last 5 ratings:
  2026-05-25T22:01:35.391Z  magic-ui                        5/5
  2026-05-25T22:01:35.049Z  shadcn-ui-mcp                   4/5
  2026-05-25T22:01:34.739Z  frontend-design                 3/5
==================================================
```

## Commands

ToolForge ships 4 slash commands:

| Command | Purpose |
|---|---|
| `/toolforge <category>` | Discover and install top 5 tools for the category. Valid: UI, backend, database, testing, devops. |
| `/toolforge-status` | Show install count, top 5 rated tools, last 5 ratings. |
| `/toolforge-rate <1-5>` | Rate the most recently installed tool on a Likert scale. |
| `/toolforge-rescan` | Clear the 5-minute local-scan cache so the next `/toolforge` invocation rebuilds the local source index from disk. |

## How live discovery works

When you run `/toolforge UI`, the `toolforge-curator` skill is invoked. It:

1. Runs two `WebSearch` queries in parallel: a general query and a `site:github.com topic:mcp-server <category>` query.
2. Picks the 3 to 5 most promising URLs.
3. Runs `WebFetch` against each, locked to an allow-list of 7 trusted hosts: `github.com`, `raw.githubusercontent.com`, `claudemarketplaces.com`, `modelcontextprotocol.io`, `aitmpl.com`, `npmjs.com`, `www.npmjs.com`. The lock is what blocks SEO spam.
4. Parses each result for name, install command, stars, last commit date, and a one-line description.
5. Looks up the historical Likert average for each candidate from the local SQLite store via one bulk shell-out.
6. Computes a composite score blending log-normalized stars, exponential recency with a 180-day half-life, and a Bayesian-shrunk Likert average (prior mean 3.0, prior weight 5) that also fades old ratings on the same 180-day half-life. See `ARCHITECTURE.md` for the exact formula.
7. Returns the top 5 sorted by score, with install commands ready to copy.

Note on `claude mcp add`: the CLI requires a `--` separator between its own flags and the wrapped install command (for example `claude mcp add foo -- npx -y some-pkg`). The curator and the offline fallback entries both write the `--` separator explicitly so the install line works as-is when you paste it.

If the live pipeline returns fewer than 5 valid candidates or anything takes longer than 10 seconds, ToolForge falls back to a hand-curated offline cache in `fallback/<category>.json` (5 known good entries per category). The demo still runs with the network cable unplugged.

## Local sources

Live discovery is half the picture. The other half is what is already on your machine. In parallel with `WebSearch`, the curator skill runs `bin/toolforge_local_scan.py`, which produces a ranked list of locally-available candidates per category and folds them into the same bulk DB lookup and composite scoring as live entries.

What it scans, in order:

1. `claude plugin list` and `claude mcp list`: already-installed plugins and MCP servers. These get an `[installed]` badge in the output and a `+0.10` visibility bonus in the composite score so you see what you already have before being asked to install something new.
2. `~/.claude/skills/` and `~/.claude/agents/`: user-wide skills and agents.
3. `<cwd>/.claude/skills/` and `<cwd>/.claude/agents/`: project-scoped skills and agents.
4. Any absolute paths listed in the `local_paths` array of `~/.claude/toolforge-config.json`. This is the opt-in slot for reference repositories, internal shared catalogs, or a hand-built skill garden. The plugin ships no defaults for this list. The intent is that ToolForge is not a hardcoded catalog; users opt in to local repos by editing this file.

Per-entry schema for local candidates: `name`, `type`, `source`, `path`, `installed`, `description`, `category_score`, `stars_norm` (fixed at 0.4 for local entries since star counts do not apply), `recency_norm` (exponential decay from `git log -1 --format=%ct` when the source is a git repo, otherwise file mtime), and `category`. Categorization is keyword-based with per-category keyword sets, a drop threshold of 0.3, and a cap of 10 entries per category. See `ARCHITECTURE.md` for the verbatim keyword lists and the security caps that bound the scan.

Results are cached at `tempdir/toolforge_local_scan_<category>.json` for 5 minutes. To force a refresh (after installing a new plugin, deleting a local skill, or editing `local_paths`), run `/toolforge-rescan`.

Sample `~/.claude/toolforge-config.json`:

```json
{
  "local_paths": [
    "/home/me/code/internal-skills",
    "/home/me/code/reference-repos/awesome-claude-skills"
  ]
}
```

If the config file is missing, malformed, or unreadable, the scanner falls back to defaults silently and prints a one-line stderr warning. The `local_paths` entries are canonicalized before scanning; path-escape attempts via `..` or symlinks are dropped, not followed.

## How the Likert learning loop works

1. A `PostToolUse` hook increments a per-session counter on every `Edit`, `Write`, or `Bash` tool call.
2. On `SessionEnd`, if the session crossed 5 tool calls, ToolForge emits a friendly prompt asking the user to rate the most recently installed tool with `/toolforge-rate <1-5>`.
3. The rating is written to a `ratings` table in `~/.claude/toolforge.db`.
4. The next time `/toolforge` runs for any category, the composite score pulls the Bayesian-shrunk decayed average per candidate and re-ranks accordingly.

That is the self-learning loop. No ML, no cloud, no accounts. Just SQL.

## Security

URLs are gated by a hard allow-list locked to the 7 trusted hosts listed above, enforced by `bin/toolforge_validate_url.py` (IDN canonicalization, control-byte rejection, scheme check). Prompt-injection defense requires re-validating any URL the model discovers inside a fetched page (README links, redirects, "see also" pointers) before passing it to a second `WebFetch`. Instructions inside fetched content that ask the curator to widen `allowed_domains` are ignored.

Install commands are validated by an argv allow-list (only known-good binaries like `npx`, `pip`, `claude`, `uvx`), executed with `shell=False`, and resolved through `shutil.which` with refusal for user-writable bins to defeat PATH hijacking. Tool names are constrained to `^[a-z0-9._@/-]{1,80}$` upstream and at the database write boundary. See `ARCHITECTURE.md` for the full security table.

## Offline fallback

Every category has a 5-entry JSON cache at `fallback/<category>.json`. The cache is intentionally small. If live discovery fails or times out, ToolForge surfaces these 5 instead. You will see a one-line notice: "Live discovery unavailable, falling back to cached results." The fallback files are integrity-checked against `fallback/manifest.sha256` before they are loaded; a mismatch refuses to load rather than silently running a tampered install command.

## Storage

All ToolForge data lives in `~/.claude/toolforge.db`:

- `installs` table: tool_name, category, approved, installed_at
- `ratings` table: tool_name, rating (1 to 5), rated_at

To inspect manually:

```
python toolforge/bin/toolforge_db.py status
```

To reset:

```
rm ~/.claude/toolforge.db
```

## Roadmap

### v0.2

- Proactive scanning every other day: a cron-friendly script that refreshes a local catalog and surfaces newly trending tools without waiting for `/toolforge` to be invoked.

  Example cron line on macOS or Linux:

  ```
  0 9 */2 * * python ~/.claude/plugins/toolforge/bin/toolforge_scan.py
  ```

### v0.3

- Cloud sync of ratings (opt-in, anonymous): aggregate Likert ratings across users so the cold-start ranking is already useful.
- ML-based ranking: replace the hand-tuned weights with a small learned ranker that adapts per category and per user.

### v0.4

- Per-user telemetry opt-in for live trending discovery (still no accounts; uses anonymized hash IDs).

### Explicit non-goals for v1

- Multi-user accounts. SQLite local only.
- Auto-application orchestration. Claude Code already activates installed plugins on the next message.
- Custom TUI. Plain stdout is fine.
- More than 5 categories. UI, backend, database, testing, devops cover the demo.

## Companion docs

- `ARCHITECTURE.md`: full ranking formula, security table, data flow.
- `TROUBLESHOOTING.md`: common install and runtime issues.
- `CHANGELOG.md`: version history.
- `CONTRIBUTING.md`: how to add a category or extend the curator.
- `demo/demo_script.md`: the live demo walkthrough.

## License

MIT.
