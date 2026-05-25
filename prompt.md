# ToolForge Build Prompt

Paste this into Claude Code in a fresh repo. It is written assuming the agent has filesystem access, shell access, and WebSearch.

---

## Role

You are a senior plugin engineer building a Claude Code plugin called **ToolForge**. You optimize for a clean, polished v1 that demos well against vanilla Claude Code. You write simple, readable code. You do not use em dashes. You do not over engineer. You stay sequential: do not start the next phase until the current one works end to end.

## Product

ToolForge is a Claude Code plugin that combines live tool discovery, smart installation, and performance feedback in one slash command flow.

The user runs a command like `/toolforge UI` (or backend, database, testing, devops). ToolForge searches the web in real time for the top plugins, MCP servers, and skills for that task type, ranks them, and surfaces the top 5. The user approves an install with a single keystroke. ToolForge then logs a session and after 5 tool calls asks the user to rate the result on a 1 to 5 Likert scale. Ratings feed a SQLite store. Future `/toolforge` calls boost or penalize results based on historical satisfaction.

The pitch is: Anthropic Tool Search only filters tools you already have installed. ToolForge finds the tools worth installing in the first place, then learns which ones actually help you.

## Demo target

The end state is a working side by side demo against vanilla Claude Code. Same repo, same prompt: "Build me a pricing card component."

- Left terminal: vanilla Claude Code, produces a generic result.
- Right terminal: Claude Code with ToolForge installed. User runs `/toolforge UI`, sees live web results, approves install of shadcn-ui MCP and magic-ui skill, then runs the same prompt and produces a polished result.
- Demo closes with `/toolforge-status` showing the just-given Likert rating reordering the rankings.

Build for this demo. Every feature should either directly enable a moment in this demo or be cuttable.

## Prerequisite skills (install before Phase 1)

Three official Anthropic plugins must be installed and verified working before you write any ToolForge code. These accelerate the entire build and enforce best practices.

1. **plugin-dev** (from `anthropics/claude-code/plugins/plugin-dev`)

   Bundles 7 skills you will use throughout: hook-development, mcp-integration, plugin-structure, plugin-settings, command-development, agent-development, skill-development. Also includes `/plugin-dev:create-plugin` for guided workflows and `plugin-validator` and `skill-reviewer` agents that lint your work.

2. **skill-creator** (from `anthropics/claude-plugins-official/plugins/skill-creator`)

   Provides Create, Eval, Improve, and Benchmark modes for authoring skills. You will use this heavily for the curator skill in Phase 2, since the curator prompt is the heart of the product. The skill-creator knows the critical detail that skills tend to undertrigger and must be written with concrete, pushy trigger phrases.

3. **Superpowers** (from `claude.com/plugins/superpowers`)

   Enforces TDD red-green-refactor, structured debugging, and Socratic brainstorming. Use the TDD discipline to enforce the verify-before-moving-on checkpoints in this build.

Install command pattern:
```
claude plugin marketplace add <repo-url>
claude plugin install plugin-dev@anthropics
claude plugin install skill-creator@anthropics
claude plugin install superpowers@anthropics
```

Verify all three trigger correctly before proceeding. Run `/plugin-dev:create-plugin` once to confirm plugin-dev is active.

## Tech stack

- Python stdlib only for hooks and scripts, using UV single file script format
- SQLite via stdlib `sqlite3` for storage at `~/.claude/toolforge.db`
- Markdown for slash command and skill definitions
- Live discovery via Claude Code's built in WebSearch and WebFetch tools, invoked from inside the curator skill prompt
- Tiny offline fallback cache (5 tools per category) for network failure scenarios
- Plugin distribution as a local marketplace via `claude plugin marketplace add`

Do not introduce frameworks, ORMs, or build tools. Plain Python, plain JSON, plain SQL, plain markdown.

## File structure to create

```
toolforge/
  .claude-plugin/
    plugin.json
    marketplace.json
  commands/
    toolforge.md
    toolforge-status.md
    toolforge-rate.md
  hooks/
    post-tool-use-counter.py
    session-end-likert.py
  skills/
    toolforge-curator/
      SKILL.md
  fallback/
    ui.json
    backend.json
    database.json
    testing.json
    devops.json
  bin/
    toolforge_install.py
    toolforge_db.py
  README.md
  demo/
    demo_script.md
```

Notice there is no `toolforge_search.py`. The curator skill is the search engine.

## Build phases

Sequential. Do not move to the next phase until the current one works end to end and you have verified it manually.

### Phase 1: Plugin skeleton

**Skills to use:** `plugin-structure`, `command-development` (both from plugin-dev). Run `plugin-validator` agent at the end.

1. Create `plugin.json` with name, version, description, and components map pointing to commands, hooks, skills.
2. Create `marketplace.json` so the plugin can be installed locally via `claude plugin marketplace add ./toolforge`.
3. Create `commands/toolforge.md` with a prompt template that takes a category argument and instructs the agent to invoke the toolforge-curator skill with that category.
4. Verify install with `claude plugin install toolforge@local-toolforge` and confirm `/toolforge` autocompletes inside Claude Code.

**Verify before moving on:** `/toolforge UI` triggers without errors, even if it returns nothing yet. `plugin-validator` agent passes.

### Phase 2: Curator skill (the heart of the product)

**Skills to use:** `skill-creator` in Create mode for the initial draft, then Eval mode to test against sample queries, then Improve mode to iterate. Also reference `skill-development` (from plugin-dev) for trigger phrasing and progressive disclosure patterns.

Write `skills/toolforge-curator/SKILL.md` as a detailed prompt that tells Claude exactly how to discover tools. The skill must:

1. Take a category argument (UI, backend, database, testing, devops).
2. Use WebSearch to query: `top Claude Code plugins MCP servers skills for {category} 2026`, plus a second targeted query: `site:github.com topic:mcp-server {category}`.
3. Use WebFetch on the 3 to 5 most promising URLs, with `allowed_domains` limited to `github.com`, `claudemarketplaces.com`, `modelcontextprotocol.io`, `aitmpl.com`, `npmjs.com`. Locking the domain list is what blocks SEO spam.
4. Parse each result for: name, install command, GitHub stars, last commit date, one line description.
5. Shell out to `bin/toolforge_db.py get_avg_rating <name>` for each candidate. Inject the stored Likert average as a re-ranking signal.
6. Compute a composite score: stars weighted 0.3, recency weighted 0.3, historical Likert weighted 0.4.
7. If WebSearch returns fewer than 5 valid results or any step times out after 10 seconds, load `fallback/{category}.json` and surface those instead.
8. Return the top 5 as a clean numbered list with install commands ready to copy.

Make the skill description pushy with explicit trigger phrases. Include one worked example inside the skill prompt so the model has a reference pattern.

Run skill-creator's Eval mode with at least 5 sample queries (one per category) before moving on. Run Improve mode if any eval fails.

**Verify before moving on:** `/toolforge UI` returns 5 real, relevant, distinct tools sourced live from the web. Eval pass rate is 100 percent.

### Phase 3: Fallback cache

Hand write `fallback/ui.json` with 5 known good entries (shadcn-ui MCP, magic-ui skill, frontend-design skill, tweakcn, aceternity components). Repeat 5 entries each for backend, database, testing, devops. Each entry: `{name, type, source_url, stars, install_command, description}`.

**Verify before moving on:** With wifi off, `/toolforge UI` still returns 5 tools from the fallback.

### Phase 4: Install flow and database

**Skills to use:** `command-development` (from plugin-dev) for updating the toolforge command to chain into the installer.

1. Write `bin/toolforge_db.py` with: `init_db()`, `log_install(tool_name, category, approved)`, `log_rating(tool_name, rating)`, `get_avg_rating(tool_name) -> float`. Initialize on first import.
2. Write `bin/toolforge_install.py` that takes a tool name and install command, prompts the user "Install? [y/n]" via stdin, runs the shell command if approved, and logs the install.
3. Update `commands/toolforge.md` so the agent calls `toolforge_install.py` after the skill returns its top 5.

**Verify before moving on:** Running `/toolforge UI`, picking option 1, and confirming the install actually runs `claude plugin install` and creates a row in the SQLite database.

### Phase 5: Likert feedback hook

**Skills to use:** `hook-development` (from plugin-dev). This is exactly its specialty. Reference the schema for PostToolUse and SessionEnd events, and use the `${CLAUDE_PLUGIN_ROOT}` pattern for hook script paths.

1. Write `hooks/post-tool-use-counter.py` as a UV single file script. It reads PostToolUse JSON from stdin, increments a session counter file at `/tmp/toolforge_session_<session_id>.count`, and exits 0.
2. Write `hooks/session-end-likert.py` as a UV single file script. It reads the counter, and if the count is 5 or more, prints a clean 1 to 5 rating prompt to stdout using `additionalContext`. It reads the most recently installed tool from SQLite to anchor the rating.
3. Register both hooks in `plugin.json` under `hooks` with matchers `Edit|Write|Bash` for PostToolUse and no matcher for SessionEnd.
4. Add a `/toolforge-rate <1-5>` slash command that writes the rating to SQLite for the last installed tool.

Run the plugin-dev `test-hook.sh` and `validate-hook-schema.sh` utility scripts to confirm correctness.

**Verify before moving on:** After 5 tool calls, ending a session triggers the rating prompt, and `/toolforge-rate 5` writes the row.

### Phase 6: Status command and learning visibility

**Skills to use:** `command-development`.

1. Build `/toolforge-status` to print: total installs, top 5 rated tools, last 5 ratings, current session count. Format with simple ASCII so it reads cleanly in the terminal.

**Verify before moving on:** After rating a tool, running `/toolforge UI` shows that tool ranked higher than before. Show this works.

### Phase 7: Demo materials

1. Create a fresh demo repo with one empty React component file: `PricingCard.jsx`.
2. Write `demo/demo_script.md` with exact commands, expected outputs, and timing for the side by side demo.
3. **Strategic note on `frontend-design` skill.** This is an official Anthropic skill that auto-invokes for frontend work. Do not install it on the vanilla terminal in the demo, since it would narrow the visual gap with ToolForge. Either leave it off both, or install it as part of the live ToolForge install moment to make the right side jump even further.
4. Practice end to end at least 10 times.
5. Record a clean backup video of the full demo using OBS, in case live fails on stage.
6. Practice once with the network unplugged to confirm the fallback path works under the lights.

### Phase 8: Documentation

Write `README.md` covering: what ToolForge does in 3 sentences, install command, the 5 slash commands, how live search works, how the Likert loop works, the offline fallback note, and a roadmap section for v0.2 (proactive scanning) and v0.3 (cloud sync, ML ranking).

## What v1 does not include

These are explicit non-goals. Defer to v0.2 or later. Mention them in the README roadmap.

1. **Proactive scanning every other day.** Cron config snippet in the README is enough.
2. **ML based ranking.** SQL ranking by average Likert is sufficient and honest.
3. **More than 5 categories.** UI, backend, database, testing, devops covers the demo.
4. **Auto application orchestration.** Claude Code activates installed plugins automatically on the next message. Do not reinvent that.
5. **Multi user, cloud sync, accounts.** Local SQLite only.
6. **Custom UI or TUI.** Plain stdout is fine.

## Raw repos to reference if a skill is insufficient

The prerequisite skills above cover most of what you need. These repos are deeper backup material if a skill leaves you uncertain on a detail.

1. `disler/claude-code-hooks-mastery` for the UV single file Python hook pattern in action.
2. `karanb192/claude-code-hooks` for prewritten hook script examples.
3. `anthropics/skills` for canonical SKILL.md examples to mirror.
4. `mcpm.sh` v2 for profile architecture inspiration, but do not implement profiles in v1.

## Anti-patterns to avoid

- Do not hard code a discovery catalog. The curator skill plus WebSearch is the product. The fallback file is only for offline emergencies.
- Do not build a TUI.
- Do not add OAuth, accounts, or cloud sync.
- Do not write a custom web scraper or HTML parser. Use WebFetch with the markdown extraction method and let Claude parse the result.
- Do not write your own plugin installer. Wrap the existing `claude plugin install` command.
- Do not invent a new manifest format. Use the official `.claude-plugin/plugin.json` schema exactly.
- Do not skip the prerequisite skill installs. They are not optional.
- Do not use em dashes anywhere in code comments, README, skill prompts, or demo script.

## Code standards

- Functions under 30 lines where possible
- Type hints on all Python function signatures
- No dependencies outside Python stdlib
- One concern per file
- Print statements with clear labels, not raw dumps
- Errors fail loud, not silent
- Test each file in isolation before wiring it into the plugin

## Pitch script anchor

Demo opens with: "Anthropic Tool Search picks from the tools you already installed. It does not help you find new ones. We do, live from the web, and we learn which ones actually help you."

Demo closes with: "Watch the rating change the ranking." Run `/toolforge-status` and show that shadcn-ui MCP just jumped to the top because it got a 5 rating. That is the self learning visible in 10 seconds.

---

## Expected output

When you finish this prompt, the following must exist in the repo and be verified working:

1. **Prerequisite skills installed.** plugin-dev, skill-creator, and Superpowers are all installed and verified triggering correctly.
2. **Installable plugin.** Running `claude plugin marketplace add ./toolforge` then `claude plugin install toolforge@local-toolforge` succeeds with no errors.
3. **All five slash commands work:** `/toolforge <category>`, `/toolforge-status`, `/toolforge-rate <1-5>`. Each verified manually.
4. **Live web search returns real tools.** `/toolforge UI` shows 5 tools pulled live from github.com or the other allowed domains, each with a working install command.
5. **Curator skill passes evals.** skill-creator Eval mode reports 100 percent pass rate across at least 5 sample queries.
6. **Offline fallback works.** With network disabled, `/toolforge UI` still returns 5 tools from the fallback JSON.
7. **Install flow works end to end.** Picking a tool runs the real `claude plugin install` command and the install is logged in SQLite.
8. **Likert hook fires correctly.** After 5 tool calls, ending the session triggers a rating prompt, and the rating is stored. Hook validation utilities pass.
9. **Self learning is visible.** Rating a tool 5 stars then re-running `/toolforge` for the same category shows that tool ranked higher.
10. **Demo materials ready.** `demo/demo_script.md` contains a tested step by step script. A backup video has been recorded.
11. **README is complete.** Covers install, usage, all 5 commands, the live search and Likert systems, and the roadmap.
12. **Side by side demo rehearsed.** You have run it end to end at least 10 times with successful results.

When all 12 items are checked, stop and report back with a short summary of what was built and any deviations from this spec.

## Start now

Begin by installing the three prerequisite skills. Do not write any ToolForge code until they are verified working. Then proceed to Phase 1.