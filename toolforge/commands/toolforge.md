---
description: Discover top Claude Code plugins, MCP servers, and skills for a category (UI, backend, database, testing, devops). Live web search plus Likert-rated re-ranking.
argument-hint: <category>
---

You are running the `/toolforge` command. The user passed the category argument: **$ARGUMENTS**.

Your job: perform live tool discovery for that category, surface the top 5 ranked results, then offer to install the user's pick.

## Step 1: Invoke the curator skill

Invoke the `toolforge-curator` skill with the category `$ARGUMENTS`. The skill:

1. Runs two `WebSearch` queries in parallel.
2. Validates every URL via `${CLAUDE_PLUGIN_ROOT}/bin/toolforge_validate_url.py` (hard allow-list gate).
3. `WebFetch`es the top 3 to 5 surviving URLs (allow-list locked).
4. Parses name, install command, stars, last commit, description.
5. Runs ONE bulk shell-out: `python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_db.py" get_rating_stats_bulk <names>`.
6. Composite scoring: log-stars (0.3) + exp-recency (0.3) + Bayesian-shrunk Likert (0.4).
7. Falls back fully, or partial-merges with `fallback/$ARGUMENTS.json` if live results are <5.
8. Returns the top 5 with install commands.

If `$ARGUMENTS` is not one of UI, backend, database, testing, devops, tell the user the supported categories and stop.

## Step 2: Show the user the ranked list

```
Top 5 tools for {category}:

1. <name> (score: X.XX)
   <one-line description>
   Source: <url>
   Install: <install command>

...
```

## Step 3: Offer install (interactive consent in chat)

Ask the user in chat: "Which one would you like to install? Type 1 to 5, or n to skip."

When the user picks a number, you (Claude) have their confirmation. Pass `--yes` to the installer because the subprocess has no tty. Use the Bash tool with each positional argument fully quoted (do not interpolate raw user-controlled strings into a shell command):

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
