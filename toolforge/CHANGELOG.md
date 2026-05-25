# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-25

### Added

- `/toolforge <category>` slash command with live discovery via WebSearch + WebFetch
- `/toolforge-status` dashboard (total installs, top 5 Bayesian-shrunk decayed scores, last 5 ratings, current session count)
- `/toolforge-rate <1-5>` Likert rating command
- `toolforge-curator` skill with hard URL allow-list (`github.com`, `raw.githubusercontent.com`, `claudemarketplaces.com`, `modelcontextprotocol.io`, `aitmpl.com`, `npmjs.com`, `www.npmjs.com`)
- Bayesian-shrunk composite ranking (log-stars, exp-recency, Bayesian Likert with prior mean 3.0 and weight 5, half-life 180 days)
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
