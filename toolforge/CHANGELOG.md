# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `bin/toolforge_local_scan.py` for local source discovery (installed plugins via `claude plugin list`, installed MCP servers via `claude mcp list`, user-wide `~/.claude/skills` and `~/.claude/agents`, project-scoped `<cwd>/.claude/skills` and `<cwd>/.claude/agents`, and user-configured local reference repos via the `local_paths` array in `~/.claude/toolforge-config.json`).
- `/toolforge-rescan` slash command to clear the 5-minute per-category local-scan cache files at `tempdir/toolforge_local_scan_<category>.json`.
- `[installed]` badge in `/toolforge` output for tools already on the user's machine. The install command line is replaced by the badge; local-repo entries show `Local source: <path>` instead. A visibility bonus of `+0.10` is added to the composite score so installed entries appear near the top of the ranking.
- Local entries use a fixed `stars_norm = 0.4` (since star counts do not apply to local sources) and a `recency_norm` derived from `git log -1 --format=%ct <path>` when the source is a git repo, falling back to file mtime otherwise. Bayesian Likert ratings still apply on top of these signals via the same bulk DB lookup as live entries.

### Changed

- `toolforge-curator` skill bumped to version `0.3.0`. Step 1b runs local-scan in parallel with the existing WebSearch calls. Step 5's bulk DB call now includes local entries alongside live ones. Step 6's composite scoring applies the fixed `stars_norm = 0.4`, the scanner-provided `recency_norm`, and the `+0.10` visibility bonus for installed entries. Step 8's output format renders the `[installed]` badge and the `Local source: <path>` line.
- Plugin auto-registers 4 slash commands now (was 3). README and `plugin.json` updated.

### Security

- Local-scan opens metadata files with a hard 4 KiB per-file read cap, walks directories with `followlinks=False` (no symlink follow), enforces a depth cap of 4 from each scan root, a file count cap of 2000 per invocation, and an 8-second wall-clock budget.
- Subprocess invocations from the scanner are limited to exactly three binaries: `claude plugin list`, `claude mcp list`, and `git log -1 --format=%ct <path>`. Each runs via `subprocess.run(shell=False, timeout=...)` with explicit per-call timeouts (`CLAUDE_LIST_TIMEOUT_SECONDS = 5`, `GIT_LOG_TIMEOUT_SECONDS = 2`). No other executable is callable from the scanner and no shell metacharacter parsing is performed.
- User-configured `local_paths` entries are resolved with `os.path.realpath` and canonicalized before scanning. Path-escape attempts via `..` segments or symlink chains result in the entry being dropped, not silently followed outside the declared root.

### Notes

- Default scan paths intentionally exclude reference-repo collections. The plugin is not a hardcoded catalog of every popular skill on disk. Users opt in to local repos by adding absolute paths to the `local_paths` array of `~/.claude/toolforge-config.json`; if the file is missing or malformed, the scanner falls back to defaults silently with a one-line stderr warning.

## [0.1.0] - 2026-05-25

### Added

- `/toolforge <category>` slash command with live discovery via WebSearch + WebFetch
- `/toolforge-status` dashboard (total installs, top 5 Bayesian-shrunk decayed scores, last 5 ratings, current session count)
- `/toolforge-rate <1-5>` Likert rating command
- `toolforge-curator` skill with hard URL allow-list (`github.com`, `raw.githubusercontent.com`, `claudemarketplaces.com`, `modelcontextprotocol.io`, `aitmpl.com`, `npmjs.com`, `www.npmjs.com`)
- Bayesian-shrunk composite ranking (log-stars, exp-recency, Bayesian Likert with prior mean 3.0 and weight 5, half-life 75 days — AI tooling moves fast)
- `toolforge_install.py` argv allow-list validator with deny-list shell metacharacter pre-filter, head allow-list (claude, npx, uvx, npm, pip, pipx, uv), per-head subcommand allow-list, recursive validation for `claude mcp add`, `shutil.which` PATH resolution with user-writable bin refusal
- `toolforge_validate_url.py` URL allow-list gate with IDN canonicalization and control-byte filtering
- `toolforge_rate.py` rating wrapper with `^[1-5]$` regex at Python layer
- `PostToolUse` hook (`post-tool-use-counter.py`) byte-append per-session counter
- `SessionEnd` hook (`session-end-likert.py`) threshold-gated rating prompt (5 tool calls)
- SHA-256 fallback integrity check (`fallback/manifest.sha256`) blocks loading of tampered cached install commands
- Five hand-curated fallback JSON caches (UI, backend, database, testing, devops, 5 entries each)
- Demo materials: scaffold React app, end-to-end demo script, rehearsal checklist, network-off rehearsal procedure

### Security

- URL allow-list enforced via separate validator script; the curator skill cannot bypass it
- Install commands validated via argv allow-list, executed with `shell=False` and PATH resolved via `shutil.which`
- Prompt-injection defense: URLs discovered inside WebFetch results must re-pass the URL validator before any follow-up fetch

### Documentation

- README.md with install, command reference, ranking explanation, and v0.2 / v0.3 roadmap
- ARCHITECTURE.md with component map, ranking math, security boundaries, and exit-code reference
- TROUBLESHOOTING.md with common failure modes and recovery
- CONTRIBUTING.md with PR + fallback-update workflow
