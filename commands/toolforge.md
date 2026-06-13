---
description: Discover top Claude Code plugins, MCP servers, and skills for a category (UI, backend, database, testing, devops). Bare /toolforge detects your repo's stack and recommends a category. Live web search plus Likert-rated re-ranking.
argument-hint: [category]
---

You are running the `/toolforge` command. The user passed the category argument: **$ARGUMENTS**.

Your job: perform live tool discovery for that category, surface the top 5 ranked results, then offer to install the user's pick.

## Section 0: Cache note

Local-source scan is cached at `tempdir/toolforge_local_scan_<category>.json` for 5 minutes. Run `/toolforge-rescan` to clear all caches. Pass `--force` (not user-visible from this command, but the curator may auto-invoke on stale-cache detection) to bypass.

## Step 0: Zero-argument stack detection (only when $ARGUMENTS is empty)

If the user passed no category, detect their repo's stack instead of demanding one. Shell out:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_stack_detect.py" detect . --json
```

Returns `{"technologies": [...], "ranked_categories": [...], "combos": [...]}` — a declarative manifest scan (package.json deps, requirements.txt, pyproject.toml, go.mod, Cargo.toml, docker-compose.yml, config-file existence; root + 2 levels deep, no symlink follow, 5s budget). Cached 5 minutes per absolute path at `tempdir/toolforge_stack_detect_<sha1>.json`; add `--force` to bypass.

1. **Nothing detected** (`technologies` empty): say so, list the supported categories (UI, backend, database, testing, devops), and ask the user to pick one. Stop until they answer.
2. **Stack detected**: present it compactly:

   ```
   Detected stack: Next.js, Supabase, Tailwind CSS
   Recommended categories: 1. database (1.00)  2. ui (0.67)
   ```

3. Ask: "Search `<top category>` (recommended for your stack)? Or pick another: UI / backend / database / testing / devops."
4. On confirmation (or an alternate valid pick), proceed to Step 1 with that category in place of `$ARGUMENTS`, and pass the detected tech ids (e.g. `nextjs`, `supabase`, `tailwind`) to the curator as stack context — it uses them as extra WebSearch keywords (skill step 1) and as a `+0.10` stack-match bonus in composite scoring (skill section 6).

## Step 1: Invoke the curator skill

Invoke the `toolforge-curator` skill with the category `$ARGUMENTS` (or the category confirmed in Step 0 when `$ARGUMENTS` was empty, forwarding the detected tech ids as stack context). The skill:

1. Runs two `WebSearch` queries in parallel.
2. Runs `bin/toolforge_local_scan.py scan <category>` in parallel with WebSearch (cached 5 min).
3. Validates every URL via `${CLAUDE_PLUGIN_ROOT}/bin/toolforge_validate_url.py` (hard allow-list gate).
4. `WebFetch`es the top 3 to 5 surviving URLs (allow-list locked).
5. Parses name, install command, stars, last commit, description.
6. Runs ONE bulk shell-out: `python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_db.py" get_rating_stats_bulk <names>`.
7. Composite scoring: log-stars (0.3) + exp-recency (0.3) + Bayesian-shrunk Likert (0.4).
8. Falls back fully, or partial-merges with `fallback/$ARGUMENTS.json` if live results are <5.
9. Returns the top 5 with install commands.

If `$ARGUMENTS` is empty, do NOT stop — run Step 0 (stack detection) first and proceed with the confirmed category. If `$ARGUMENTS` is non-empty but not one of UI, backend, database, testing, devops, tell the user the supported categories and stop.

## Step 2: Show the user the ranked list

Entries fall into three types; format each accordingly:

- **Installed** (`installed: true`, source like `installed-plugin`, `user-skills`, etc.):
  ```
  N. <name> [installed] (score: X.XX) - <description>
     Status: already on this machine, no action needed.
  ```
- **Local-repo** (`installed: false`, `source: local-repo:<abs-path>`):
  ```
  N. <name> (score: X.XX) - <description>
     Local source: <abs-path>
     Install: <install command>
  ```
  The install command activates the local skill or plugin if the source pattern supports it; otherwise the curator omits the `Install:` line.
- **Web-discovered** (no local-scan fields):
  ```
  N. <name> (score: X.XX) - <description>
     Source: <url>
     Install: <install command>
  ```

## Step 3: Offer install (interactive consent in chat)

Ask the user: "Which to install? Type a number (1-5), a comma list (1,3,4), `all`, or `n`. (Entries marked `[installed]` are already on your machine and will be skipped automatically.)"

If the user picks **only** installed entries, respond: `Already installed. Try /toolforge <category> again and pick non-installed entries, or /toolforge-rate <1-5> to record a rating.`

For non-installed picks, you (Claude) have consent. **Always pass `--yes`** (subprocess has no tty). Route by pick count:

### Step 3-pre: Security-review handoff (web picks only)

Before any web-discovered install runs, dispatch the security subagent. For local-source picks (`source` starts with `installed-…` or `local-repo:`), skip this — they live under user-trusted paths.

For each web pick:

1. Build the prompt:
   ```
   PROMPT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_security_handoff.py" prompt "<source_url>" "<tool_name>")
   ```
2. Call the Task tool with `subagent_type: general-purpose` and the captured `$PROMPT` as the prompt. The subagent uses WebFetch (allow-list locked) to read the repo, scans for malware, optionally proposes fixes, and returns a single-line JSON verdict.
3. Parse the verdict from the subagent's final message:
   - `clean` → proceed to Step 3a/3b.
   - `suspect` → show the user the `summary` + first 3 findings; ask `Install anyway? [y/N]`. Default no. Only proceed on explicit yes.
   - `malicious` → REFUSE; do NOT call the installer; surface the findings.

If the user picks multiple tools in one batch, run the handoff subagents IN PARALLEL (single tool-use block with N Task calls).

### Step 3a: Single pick → single shellout

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_install.py" "<tool_name>" "<install_command>" "<category-lower>" --yes
```

Quote each positional. Lowercase the category (DB regex `^[a-z]{1,32}$`).

### Step 3b: Multi-pick (`1,3,4` or `all`) → BATCH MODE: one permission prompt, one shellout

Build a JSON array of the picked non-installed tools and pipe to `--batch`. ONE Bash call → ONE permission prompt → all commands validated up-front (fail-fast — if any one is rejected, the WHOLE batch aborts with exit 2 before anything runs) → sequential execution with per-tool stdout/stderr forwarded and per-tool audit log. Use a heredoc so install_command strings never need shell re-escaping:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_install.py" --batch --yes <<'EOF'
[
  {"tool_name": "mcp-ui",            "install_command": "npm install @mcp-ui/server",                     "category": "ui"},
  {"tool_name": "magic-mcp",         "install_command": "claude mcp add magic -- npx -y @magicuidesign/mcp", "category": "ui"},
  {"tool_name": "aceternity-ui-mcp", "install_command": "claude mcp add aceternity -- npx -y aceternityui-mcp", "category": "ui"}
]
EOF
```

Rules for building the JSON:
- Lowercase the category.
- Cross-check installed state (`claude mcp list`, `claude plugin list`, etc.) BEFORE building. Skip any tool already installed — do not put a redundant install in the batch.
- If the user typed `all`, expand to the non-installed subset (not the literal top-5).
- Batch exit codes: 0 all ok, 2 validation refused (none ran) or user said no, 3 audit-log drop on success path, 4 any child install exited nonzero. Forward stderr verbatim.

### What the installer enforces (single OR batch)

- Refuse any command containing shell metacharacters (`;`, `&`, `|`, backtick, `<`, `>`, newlines).
- `shlex.split` and require `argv[0]` in allow-list (`claude`, `npx`, `uvx`, `npm`, `pip`, `pipx`, `uv`).
- Resolve via `shutil.which`; reject user-writable PATH dirs (`~/.local/bin`, `%LOCALAPPDATA%`, `~/AppData/Roaming/npm`) and `node_modules/.bin`.
- Run with `shell=False`, `capture_output=True`.
- Log per-tool success or refusal to SQLite.

The semantic security gate (malware scan) lives in the curator skill's Step 3-pre handoff, NOT inside the installer. The installer is the syntactic boundary; the handoff is the semantic one.

## Step 4: Remind the user to rate

After install, tell the user: "Run /toolforge-rate <1-5> after using this tool to feed your rating back into the rankings."

## Notes

- Do not invent tools. If discovery returns nothing real, the skill falls back automatically.
- Do not skip the URL validator or the install-command allow-list. They are the security boundary.
- Quoting arguments via the Bash tool is **defense in depth, not a security boundary**. The real defenses are the Python-side validators in `toolforge_install.py` and `toolforge_rate.py`. Always quote anyway, because layered defense costs nothing.
- Keep output tight. The user is on a stage.
