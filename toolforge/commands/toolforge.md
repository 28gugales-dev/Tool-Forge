---
description: Discover top Claude Code plugins, MCP servers, and skills for a category (UI, backend, database, testing, devops). Live web search plus Likert-rated re-ranking.
argument-hint: <category>
---

You are running the `/toolforge` command. The user passed the category argument: **$ARGUMENTS**.

Your job: perform live tool discovery for that category, surface the top 5 ranked results, then offer to install the user's pick.

## Section 0: Cache note

Local-source scan is cached at `tempdir/toolforge_local_scan_<category>.json` for 5 minutes. Run `/toolforge-rescan` to clear all caches. Pass `--force` (not user-visible from this command, but the curator may auto-invoke on stale-cache detection) to bypass.

## Step 1: Invoke the curator skill

Invoke the `toolforge-curator` skill with the category `$ARGUMENTS`. The skill:

1. Runs two `WebSearch` queries in parallel.
2. Runs `bin/toolforge_local_scan.py scan <category>` in parallel with WebSearch (cached 5 min).
3. Validates every URL via `${CLAUDE_PLUGIN_ROOT}/bin/toolforge_validate_url.py` (hard allow-list gate).
4. `WebFetch`es the top 3 to 5 surviving URLs (allow-list locked).
5. Parses name, install command, stars, last commit, description.
6. Runs ONE bulk shell-out: `python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_db.py" get_rating_stats_bulk <names>`.
7. Composite scoring: log-stars (0.3) + exp-recency (0.3) + Bayesian-shrunk Likert (0.4).
8. Falls back fully, or partial-merges with `fallback/$ARGUMENTS.json` if live results are <5.
9. Returns the top 5 with install commands.

If `$ARGUMENTS` is not one of UI, backend, database, testing, devops, tell the user the supported categories and stop.

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

Ask the user in chat: "Which one would you like to install? Type 1 to 5, or n to skip. (Entries marked `[installed]` are already on your machine and will be skipped.)"

If the user picks an installed entry, respond: `<name> is already installed. Try running /toolforge <category> again and pick a non-installed entry, or use /toolforge-rate <1-5> if you already have a rating to give.`

When the user picks a non-installed number, you (Claude) have their confirmation. Pass `--yes` to the installer because the subprocess has no tty. Use the Bash tool with each positional argument fully quoted (do not interpolate raw user-controlled strings into a shell command):

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_install.py" "<tool_name>" "<install_command>" "$ARGUMENTS" --yes
```

`toolforge_install.py` will:

- Refuse any command containing shell metacharacters (`;`, `&`, `|`, backtick, `<`, `>`, newlines).
- `shlex.split` and require `argv[0]` in the allow-list (`claude`, `npx`, `uvx`, `npm`, `pip`, `pipx`, `uv`).
- Resolve the executable via `shutil.which` so Windows .cmd shims are found.
- Run with `shell=False`.
- Log success or refusal to SQLite.

If the script exits non-zero, surface the stderr to the user verbatim.

## Step 4: Remind the user to rate

After install, tell the user: "Run /toolforge-rate <1-5> after using this tool to feed your rating back into the rankings."

## Notes

- Do not invent tools. If discovery returns nothing real, the skill falls back automatically.
- Do not skip the URL validator or the install-command allow-list. They are the security boundary.
- Quoting arguments via the Bash tool is **defense in depth, not a security boundary**. The real defenses are the Python-side validators in `toolforge_install.py` and `toolforge_rate.py`. Always quote anyway, because layered defense costs nothing.
- Keep output tight. The user is on a stage.
