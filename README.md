<div align="center">

# ToolForge

**The missing tool layer for Claude Code.**

*Finds what you don't have. Learns what works. Routes every prompt. Chains what you need.*

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Claude%20Code-blueviolet)](https://claude.ai/code)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![No external deps](https://img.shields.io/badge/deps-stdlib%20only-green)](pyproject.toml)

</div>

> **No ML, no cloud, no accounts. Just SQL.**

---

## What ToolForge does

Eight systems, one install:

| Feature | What it does |
|---|---|
| **Live discovery** | Searches the live web for the best skill, plugin, or MCP for any task. URL-gated to 7 trusted hosts, malware-scanned before anything runs. |
| **Cold-start catalog** | Ships 20 hand-curated, quality-gated MCP servers pre-seeded into the ranker. Brand-new users start with signal, not a blank slate. |
| **Auto-router** | Fires on every prompt before Claude sees it. Scores all installed skills with TF-IDF cosine similarity in under 80ms. Injects the right skill when a strong match exists. |
| **Pipeline orchestrator** | `/forge` decomposes any multi-phase task into an ordered skill chain, renders an ASCII plan, waits for approval, then executes step by step with context flowing between steps. Saves every chain for instant reuse. |
| **Task hunter** | `/toolforge-hunt` finds the single best tool for a specific task, installs it, and immediately starts working — no second prompt. |
| **Learning loop** | Rates every tool after each session. Bayesian-shrunk Likert scores with a 75-day exponential half-life re-rank all future discovery, routing, and pipeline results automatically. |
| **Health monitor** | Passively flags stale, dormant, archived, inactive, and low-rated tools. Surfaces replacements before they slow you down. |
| **Security model** | URL allow-list (7 hosts), install command sandbox (`argv[0]` allow-list, `shell=False`), and a semantic malware scan before any web-discovered tool is allowed to run. |

---

## Install

```bash
claude plugin marketplace add ./toolforge
claude plugin install toolforge@local-toolforge
```

Done. ToolForge auto-registers slash commands, skills, and hooks. No pip install. No config required.

---

## What you see after one session

```
================ ToolForge Status ================
Total approved installs:  3
Current session tool calls:  12

Top 5 rated tools (Bayesian-shrunk decayed score):
  shadcn-ui-mcp         score 3.62  raw avg 4.67  (3 rating(s))
  magic-ui              score 3.27  raw avg 3.50  (6 rating(s))
  playwright-testing    score 3.00  raw avg 3.00  (1 rating(s))

Last 5 ratings:
  2026-05-25T22:01:35Z  magic-ui            5/5
  2026-05-25T22:01:34Z  shadcn-ui-mcp       4/5
  2026-05-25T22:01:33Z  playwright-testing  3/5
==================================================

Health: all tools healthy.
Router index: fresh
```

The scores you see are not raw averages. They are Bayesian-shrunk Likert ratings with exponential decay (75-day half-life) so a tool's rank fades as AI tooling moves on. The same decay applies everywhere: discovery, routing, and pipeline suggestion.

---

## ToolForge vs. native tool search

Anthropic's built-in tool search works with what you already have installed. ToolForge does three things it cannot:

| | Anthropic Tool Search | ToolForge |
|---|---|---|
| Finds new tools | No — discovery is manual | Yes — live web search, gated to 7 trusted hosts |
| Learns your preferences | No | Yes — Likert ratings re-rank every future result |
| Routes prompts to the right skill | No | Yes — TF-IDF cosine similarity on every message |
| Chains skills into pipelines | No | Yes — `/forge` orchestrates multi-phase tasks |
| Works offline | Yes | Yes — 5-entry fallback cache per category |
| External dependencies | — | None — pure Python stdlib |
| Data leaves your machine | — | Never |

Same model. Same prompt. Different tool surface.

---

## Commands

| Command | What it does |
|---|---|
| `/toolforge <category>` | Discover and install the top 5 tools for a category. Valid: `UI`, `backend`, `database`, `testing`, `devops`. |
| `/toolforge-status` | Live dashboard: install count, top-rated tools, health warnings, router cache status. |
| `/toolforge-rate <1-5>` | Rate the most recently installed tool. Feeds directly into future rankings. |
| `/toolforge-rescan` | Force-refresh the local-scan cache and router index after installing or removing tools. |
| `/toolforge-hunt <task>` | Find the single best skill or MCP server for a specific task, install it, then immediately start working. |
| `/toolforge-status` | Shows health warnings for stale, archived, or low-rated tools and suggests replacements. |
| `/forge <multi-phase task>` | Decompose a complex task into a pipeline of skills, show the plan, get approval, then execute each step in sequence. |

---

## Live discovery — `/toolforge`

When you run `/toolforge UI`, the `toolforge-curator` skill fires:

1. **Two parallel WebSearch queries** — a general query and a `site:github.com topic:mcp-server UI` query.
2. **URL allow-list gate** — every discovered URL is validated against exactly 7 trusted hosts: `github.com`, `raw.githubusercontent.com`, `claudemarketplaces.com`, `modelcontextprotocol.io`, `aitmpl.com`, `npmjs.com`, `www.npmjs.com`. SEO spam that isn't on this list is dropped before it's fetched.
3. **WebFetch** the top 3–5 surviving URLs. Parse name, install command, stars, last commit date, description.
4. **Local scan in parallel** — `bin/toolforge_local_scan.py` scans already-installed plugins, user-wide skills, project-scoped skills, and any paths in `~/.claude/toolforge-config.json`. Installed tools get an `[installed]` badge and a +0.10 visibility bonus.
5. **Bulk DB lookup** — one shell-out to pull historical Likert averages for all candidates.
6. **Composite score** — `log-stars (0.30) + exp-recency (0.30) + Bayesian Likert (0.40)`. The recency and rating components both decay on a 75-day half-life. A repo last touched 3 months ago is meaningfully stale.
7. **Security handoff** — before any web-discovered tool installs, a subagent reads the repo, scans for malware, and returns a `clean / suspect / malicious` verdict. Malicious = hard refuse, no override.
8. **Top 5** sorted by composite score, install commands ready to paste.

If live discovery returns fewer than 5 valid candidates, ToolForge merges in the offline fallback cache (`fallback/<category>.json`, integrity-checked against `fallback/manifest.sha256`). The demo runs with the network cable unplugged.

---

## Auto-router — skills that find you

ToolForge v0.2 adds a `UserPromptSubmit` hook that fires before every message. It reads your prompt, scores it against every installed skill using TF-IDF cosine similarity, and injects a `<system-reminder>` nudge into the context when a strong match exists.

```
[ToolForge router] Relevant skills for this prompt:
  1. playwright-testing  (score: 0.71) — "Write browser tests..."
  2. sql-schema          (score: 0.58) — "Generate SQL schema..."
```

**Shadow mode (default)**: the router logs what it would have suggested but injects nothing. Runs for 7 days collecting data. If the false-positive rate stays below 15%, you can promote it to active mode in `~/.claude/toolforge-config.json`:

```json
{ "router_mode": "active" }
```

**Why TF-IDF, not an embedding model?** Because ToolForge runs inside every prompt, inline, with an 80ms wall-clock budget. No API call, no model load, no tokens consumed. The router adds ~0 to your context usage.

The router index rebuilds itself every hour or on `/toolforge-rescan`. Stop-word filtering removes generic programming verbs (`write`, `add`, `fix`, `create`) that would otherwise match everything.

---

## `/forge` — pipeline orchestrator

`/forge` is for tasks with multiple distinct phases: design something, then implement it, then test it, then review it.

```
/forge build a user auth system with a postgres schema, JWT endpoints,
       playwright E2E tests, and a security code review
```

ToolForge decomposes the task into 2–6 ordered sub-tasks, routes each to the best installed skill, renders a plan, and waits for your approval before touching a line of code:

```
============================================================
forge: "build user auth with schema, JWT, tests, review"
============================================================

  1. sql-schema           Design the postgres users/sessions schema  [0.72]
                        |
  2. (built-in)           Implement the JWT auth endpoints
                        |
  3. playwright-testing   Write E2E login and logout tests  [0.61]
                        |
  4. code-review          Security review of the implementation  [0.58]

  3 skill(s)  |  1 built-in step(s)

  Proceed? [Y/n]  Skip a step? (type 'skip N')  Edit? (type step number)
```

Once you approve, forge executes each step in sequence — each step receives a context summary from the previous one (capped at 500 words so the context window stays clean). On completion, the skill chain is saved to SQLite and will be suggested the next time someone asks forge for a similar task.

**The seven phases:**

| Phase | What happens |
|---|---|
| 1. Decompose | Parse for action verbs, sequential connectors, implicit phases |
| 2. Route | Score each sub-task against installed skills in parallel |
| 3. History | Check if this exact skill chain has run before; offer to reuse the saved template |
| 4. Plan | Render the ASCII flowchart and wait for Y/n/skip/edit |
| 5. Install | If any required skills are missing, security review + install in one batch |
| 6. Execute | Run each step in sequence with context flowing between them |
| 7. Save | Persist the chain; prompt to rate each skill used |

Forge never starts work before phase 4. Decompose and plan first, always.

---

## `/toolforge-hunt` — task-specific search

`/toolforge-hunt` is for when you know exactly what you need to do but don't have the right tool yet.

```
/toolforge-hunt animate a React component with GSAP scroll triggers
```

The hunter runs three parallel targeted WebSearch queries, fetches and parses the top results, and scores candidates by task relevance (40% capability match, 25% stack fit, 20% stars + recency, 15% Likert history). It surfaces the top 3 candidates, runs the security review, installs your pick, and immediately starts working on the task — no second prompt needed.

Use `/toolforge-hunt` for targeted one-off capability acquisition. Use `/forge` when the task has multiple phases.

---

## The learning loop

Every session:

1. A `PostToolUse` hook counts every `Edit`, `Write`, and `Bash` call.
2. On `SessionEnd`, if the session crossed 5 tool calls, ToolForge asks you to rate the most recently installed tool with `/toolforge-rate 1-5`.
3. The rating is written to `~/.claude/toolforge.db`.
4. The next `/toolforge` run pulls the Bayesian-shrunk decayed average for every candidate and re-ranks accordingly.

**Bayesian shrinkage** means a tool with two 5-star ratings doesn't leapfrog a tool with forty 4-star ratings. The prior is mean 3.0, weight 5 — a new tool starts in the middle and earns its rank through evidence. **Exponential decay** (75-day half-life) means a 5-star rating from last year carries about one-quarter the weight of one from this week.

No ML. No cloud sync. No accounts. The entire learning system is three SQL queries.

---

## Health monitoring

`/toolforge-status` runs a passive health scan (cached 6 hours) and flags:

| Flag | Meaning |
|---|---|
| `stale` | Tool hasn't been used in 90+ days |
| `dormant` | Tool was installed but never used in any session |
| `low-rated` | Bayesian average < 2.5 with at least 3 ratings |
| `archived` | Tool's repo is marked archived or deprecated |
| `inactive` | Upstream repo has been silent for 90+ days |

When flags are found, `/toolforge-status` suggests `/toolforge-hunt <task>` to find a replacement.

---

## Security

<details>
<summary>URL allow-list, install sandbox, and prompt-injection defense</summary>

### URL allow-list

Every URL — whether from WebSearch results, README links, or "see also" pointers inside fetched pages — is validated by `bin/toolforge_validate_url.py` before WebFetch is called. The allow-list is exactly 7 hosts:

- `github.com`
- `raw.githubusercontent.com`
- `claudemarketplaces.com`
- `modelcontextprotocol.io`
- `aitmpl.com`
- `npmjs.com`
- `www.npmjs.com`

The validator performs IDN canonicalization, rejects control bytes, enforces HTTPS-only, and strips any URL the model discovers inside fetched content before re-validating it. Instructions inside a fetched README that tell the curator to widen `allowed_domains` are silently ignored.

### Install command sandbox

Install commands go through `bin/toolforge_install.py`:

1. Reject any command containing shell metacharacters (`;`, `&`, `|`, backtick, `<`, `>`, newlines).
2. `shlex.split` and require `argv[0]` in the explicit allow-list: `claude`, `npx`, `uvx`, `npm`, `pip`, `pipx`, `uv`.
3. Resolve through `shutil.which` and reject user-writable PATH locations (`~/.local/bin`, `%LOCALAPPDATA%`, `~/AppData/Roaming/npm`, `node_modules/.bin`).
4. Run with `shell=False`, `capture_output=True`.
5. Log the result (approved or refused) to SQLite before executing.

Batch installs are fail-fast: if any command in the batch fails validation, the entire batch aborts before any installer runs.

### Semantic security handoff

Before any web-discovered tool installs, a subagent reads the repo, scans for known malware patterns, and returns `clean / suspect / malicious`:

- `clean` → proceed
- `suspect` → show the user the first 3 findings; ask for explicit confirmation; default no
- `malicious` → hard refuse; no override path

Local-source tools (already-installed plugins, user-wide skills, project-scoped skills) skip the handoff — they live under user-trusted paths.

### Auto-router injection safety

Descriptions injected into `<system-reminder>` tags by the auto-router are sanitized with a strict character allow-list (`[A-Za-z0-9 ._,;:!?()-/]`), capped at 100 chars each, and stripped of any `<system-reminder>` tags before injection. The total injection is capped at 500 chars and the router always returns exit 0 — it never blocks a prompt.

</details>

---

## Local sources

<details>
<summary>What gets scanned, in what order, and how to add custom paths</summary>

`bin/toolforge_local_scan.py` runs in parallel with WebSearch on every `/toolforge` invocation. It scans:

1. **`claude plugin list` and `claude mcp list`** — already-installed plugins and MCP servers. These get an `[installed]` badge and a +0.10 visibility bonus in the composite score.
2. **`~/.claude/skills/` and `~/.claude/agents/`** — user-wide skills and agents.
3. **`<cwd>/.claude/skills/` and `<cwd>/.claude/agents/`** — project-scoped skills and agents.
4. **`local_paths` in `~/.claude/toolforge-config.json`** — opt-in slot for reference repositories, internal shared catalogs, or a hand-built skill garden:

```json
{
  "local_paths": [
    "/home/me/code/internal-skills",
    "/home/me/code/reference-repos/awesome-claude-skills"
  ]
}
```

Path entries are canonicalized before scanning. `..` traversal and symlinks that escape the declared path are dropped, not followed. If the config file is missing or malformed, the scanner falls back to defaults silently.

Results are cached at `tempdir/toolforge_local_scan_<category>.json` for 5 minutes. Run `/toolforge-rescan` to force a rebuild.

</details>

---

## Storage

All ToolForge data lives in `~/.claude/toolforge.db` (SQLite, schema v3):

| Table | Contents |
|---|---|
| `installs` | tool_name, category, approved, installed_at |
| `ratings` | tool_name, rating (1–5), rated_at |
| `usage_stats` | tool_name, session_id, call_count, session_date |
| `routing_scores` | prompt_hash, skill_name, score, routed_at |
| `pipelines` | task_desc, steps_hash, steps_json, success, run_at |

```bash
# Inspect
python toolforge/bin/toolforge_db.py status

# Reset everything
rm ~/.claude/toolforge.db
```

The `pipelines` table lets forge recognize when it has run a skill chain before and offer the saved template instead of re-routing from scratch. Pipeline similarity is keyed by `steps_hash`, a 16-char SHA-256 of the ordered skill-name chain — so `[code-review, playwright-testing]` and `[playwright-testing, code-review]` are different keys.

---

## Companion docs

| File | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Full ranking formula, composite score weights, security table, data flow diagram |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common install and runtime issues |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to add a category or extend the curator |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [SKETCHY\_CODE\_AUDIT.md](SKETCHY_CODE_AUDIT.md) | Known issues, doc/code drift, future-risk spots |
| [demo/demo\_script.md](demo/demo_script.md) | Live demo walkthrough with speaker notes |

---

## License

[MIT](LICENSE)
