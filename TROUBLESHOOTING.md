# ToolForge Troubleshooting

Real failure modes, grounded in the actual code paths. Cross-references:
[ARCHITECTURE.md](./ARCHITECTURE.md) for system shape, [CONTRIBUTING.md](./CONTRIBUTING.md)
for maintenance procedures (fallback regeneration, schema bumps, allow-list edits).

## Quick diagnostic

When something looks wrong, run these five commands first. They cover 90% of root
causes (DB state, allow-list drift, install-validator regressions, MCP registration
state, plugin enablement).

```bash
python toolforge/bin/toolforge_db.py status
python toolforge/bin/toolforge_validate_url.py --list
python toolforge/bin/toolforge_install.py --self-test
claude mcp list
claude plugin list
```

What to look for:

- `toolforge_db.py status` prints total approved installs, the current session's
  tool-call count (derived from the per-session counter file), and the top 5
  Bayesian-shrunk ranked tools. Exit 3 here means SQLite is unhappy.
- `toolforge_validate_url.py --list` dumps the URL allow-list. If a host you
  expect (e.g., `www.npmjs.com`) is missing, that is the cause of "URL refused".
- `toolforge_install.py --self-test` exercises the install-command validator
  against a representative suite (explicit `--` separator, deny-list flag in
  nested payload, bad server name, etc.). Any FAIL here is a real regression.
- `claude mcp list` reflects the MCP servers Claude Code has actually
  registered. New servers do not appear until you restart Claude Code.
- `claude plugin list` confirms the ToolForge plugin itself is enabled.

---

## Failure modes

### `/toolforge UI` returns 0 results and does not fall back

**Cause:** The SHA-256 integrity check on `fallback/manifest.sha256` failed.
Someone edited a `fallback/<category>.json` (typo fix, new entry, package rename)
without regenerating the manifest, so the curator skill refuses to load
potentially tampered install commands.

**Fix:**
1. Regenerate the manifest from the current fallback files.
   POSIX: `sha256sum fallback/*.json > fallback/manifest.sha256`
   Cross-platform Python:
   `python -c "import hashlib, pathlib; [print(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}') for p in sorted(pathlib.Path('fallback').glob('*.json'))]" > fallback/manifest.sha256`
2. Re-run `/toolforge <category>`. The integrity check should pass and the
   fallback path will load.
3. Commit the regenerated manifest alongside the JSON edit. See the fallback
   update procedure in [CONTRIBUTING.md](./CONTRIBUTING.md#updating-fallback-data).

**Why it's set up this way:** The integrity check blocks a poisoned install
command from a malicious PR silently executing on first fallback fire. The
manifest is the trust anchor.

### `/toolforge UI` hangs longer than 10 seconds

**Cause:** Live discovery (WebSearch + WebFetch + URL validation) exceeded the
10-second wall-clock budget defined in the curator skill pipeline. The skill is
supposed to auto-trigger full fallback at that point.

**Fix:**
1. Press Esc to cancel the in-flight skill call.
2. Re-run `/toolforge <category>`. The fallback path is fast (read JSON, verify
   hashes, sort) and should return in under a second.
3. If hangs repeat across categories, the skill itself is misbehaving (likely a
   prompt regression). Diff `skills/toolforge-curator/SKILL.md` against the last
   known-good revision.

**Why it's set up this way:** Live discovery is best-effort. The fallback is
authoritative when the network or the model takes too long.

### Install command refused: `command head 'X' not in allow-list`

**Cause:** `toolforge_install.py` rejected the first token of the install
command. The allow-list is intentionally narrow.

**Fix:** Only these heads are supported in v0.1:
`claude`, `npx`, `uvx`, `npm`, `pip`, `pipx`, `uv`. If the candidate's install
command uses anything else (e.g., `curl`, `bash`, `cargo`, `go install`), the
v0.1 installer cannot run it. Either rewrite to a supported invocation, or skip
the install and copy-paste manually.

**Why it's set up this way:** install_command originates from WebFetch-summarized
READMEs that may be adversarial. Allow-list (not deny-list) means new heads
require a deliberate code change, not a blocklist update race.

### Install command refused: `shell metacharacters present`

**Cause:** The `DENY_CHARS` regex in `toolforge_install.py` matched one of
`;`, `&`, `|`, backtick, `<`, `>`, `\n`, `\r`, or `\t` anywhere in the raw
command string. A fallback entry or live-scraped command tried to chain multiple
shell commands.

**Fix:**
1. Rewrite the offending install_command into a single invocation. The validator
   runs before `shlex.split`, so even quoted metacharacters are rejected.
2. If the upstream really needs multiple steps (e.g., `npm install foo && npm
   run setup`), chain them via Claude Code itself across two messages, not via
   the installer.
3. If this came from a fallback JSON, fix the JSON and regenerate
   `fallback/manifest.sha256` (see entry 1 above).

**Why it's set up this way:** Shell metacharacters enable command injection.
`shell=False` in `subprocess.run` blocks the obvious vector, but pre-shlex
rejection blocks parser-differential tricks too.

### Install command refused: `nested mcp command head 'X' not in allow-list`

**Cause:** A `claude mcp add <server> -- <cmd...>` (or the implicit-separator
`claude mcp add <server> <cmd...>`) was submitted with a nested command head
that is not in `ALLOWED_FIRST`. The installer recursively validates the wrapped
command using the same allow-list.

**Fix:** The wrapped command must start with one of `claude`, `npx`, `uvx`,
`npm`, `pip`, `pipx`, `uv`. In practice for MCP servers, that means `npx` or
`uvx`. If the upstream README documents something else (e.g., `python -m
some_server`, a custom shell script), it is not installable through ToolForge
v0.1. File an issue or run the command manually outside the installer.

**Why it's set up this way:** Recursive validation closes the loophole where
the outer command is `claude mcp add` (allowed) but the inner command is
arbitrary code (would otherwise execute unchecked).

### `claude mcp add` succeeded but the server is missing from `claude mcp list`

**Cause:** This is not a ToolForge bug. Claude Code requires a session restart
before newly-added MCP servers appear in `claude mcp list` and become callable.

**Fix:**
1. Exit the current Claude Code session.
2. Re-run `claude` to start a fresh session.
3. Run `claude mcp list`. The new server should now appear.

**Why it's set up this way:** MCP servers are loaded once at session start; the
loader does not poll for new entries mid-session. Demo presenters trip on this
constantly. Worth calling out.

### `/toolforge-rate 5` reports `No installed tool found`

**Cause:** `toolforge_db.get_last_installed_tool()` returned NULL because no row
in `installs` has `approved=1`. Common reason: every previous install attempt
was refused by the allow-list (which logs `approved=0`), so there is nothing to
rate.

**Fix:**
1. Run `/toolforge <category>` and follow through to a successful install. The
   confirmation prompt must be approved and the subprocess must exit 0.
2. Verify with `python toolforge/bin/toolforge_db.py status`. The "Total
   approved installs" line should be at least 1.
3. Now `/toolforge-rate <1-5>` will attach the rating to the most recent
   approved install.

**Why it's set up this way:** Rating refused installs would pollute the ranking
signal. Only completed installs are rateable.

### SessionEnd Likert prompt does not fire after 5 tool calls

**Cause:** One of three things:
- The tool calls were not `Edit`, `Write`, or `Bash`. The PostToolUse matcher in
  `plugin.json` is `Edit|Write|Bash`. Read-only tools (Grep, Read, Glob) do not
  increment the counter.
- The per-session counter file at `${TMPDIR}/toolforge_session_<sid>.count` was
  deleted, unreadable, or never created (the post-tool-use hook may have failed
  silently on a Windows sharing violation before the linear backoff completed).
- The session did not actually end. Claude Code does not always trigger
  SessionEnd cleanly (window close vs `/exit` vs network drop behave differently).

**Fix:**
1. Trigger rating manually: `/toolforge-rate <1-5>`. The Likert prompt is just a
   nudge; the rating mechanism works without it.
2. Inspect the current count with `python toolforge/bin/toolforge_db.py status`.
   The "Current session tool calls" line reads the counter file by size.
3. If the count is 0 but you have done Edits, the PostToolUse hook is not
   firing. Check `claude plugin list` to confirm ToolForge is enabled and run a
   trivial Edit to retest.

**Why it's set up this way:** The PostToolUse matcher is narrow on purpose.
Counting every tool call (Read, Grep, etc.) would fire the Likert prompt after
trivial sessions where the user did nothing rateable.

### PostToolUse counter file grows huge across sessions

**Cause:** `post-tool-use-counter.py` calls `_prune_stale()` on every fire, but
the prune is wrapped in a swallowed `except OSError`. On a read-only tempdir or
a permission-locked tempdir, prune silently no-ops, and counter files for
crashed-out sessions accumulate.

**Fix:**
1. Manual cleanup. POSIX: `rm $TMPDIR/toolforge_session_*.count`. Windows
   PowerShell: `Remove-Item $env:TEMP\toolforge_session_*.count`.
2. The SessionEnd hook always unlinks its own counter in a `finally` block, so
   normal session exits leave no residue. Accumulation indicates abnormal exits
   (crash, kill, OS reboot mid-session).

**Why it's set up this way:** Best-effort prune cannot block the hot path. A
failing prune must not break the counter increment, so the swallow is
intentional. Periodic manual cleanup is the escape hatch.

### URL refused: `<host> not in allow-list`

**Cause:** `toolforge_validate_url.py` denied a URL. Live discovery surfaced a
domain outside the seven-entry allow-list (e.g., a Medium post, a personal blog,
a Substack writeup, a GitLab repo).

**Fix:** Not a bug. This is the security boundary. The curator skill should
simply drop the URL and move to the next candidate. If you see this message
spam in the skill output, the WebSearch results for that category are weak.
Consider adding a fallback entry for the tool to `fallback/<category>.json`
instead of widening the allow-list. See
[ARCHITECTURE.md](./ARCHITECTURE.md#url-allow-list) for the rationale on which
hosts make the cut.

**Why it's set up this way:** The allow-list is the single source of truth for
both the URL validator and the WebFetch `allowed_domains` parameter. Widening
it requires a deliberate code change and review.

### URL refused: `IDN canonicalization failed`

**Cause:** The hostname could not be encoded as IDN ASCII punycode. Either the
URL is malformed (stray bytes, broken encoding) or it is a homograph attack
(e.g., a Cyrillic character visually identical to a Latin one but encodable
under a different IDN bucket).

**Fix:** Drop the URL. Not a bug. The validator deliberately rejects rather
than guess at the user's intent. If a legitimate IDN host needs to be
allow-listed, add the punycode form (`xn--...`) to `ALLOWED_HOSTS` in
`toolforge_validate_url.py`.

**Why it's set up this way:** Visual lookalikes (`github.com` vs Cyrillic
`gіthub.com`) are a real phishing vector. Fail-closed canonicalization removes
the attack surface.

### SQLite `database is locked`

**Cause:** Another process holds the WAL writer on `~/.claude/toolforge.db`.
The connection uses WAL mode with a 3-second busy timeout
(`PRAGMA busy_timeout=3000`), which handles normal contention. A locked-error
breaks through that timeout, meaning another process held the lock for over 3
seconds.

**Fix:**
1. Close any other Claude Code sessions or terminal windows that might be
   writing to the DB. The most common culprit is a stuck install subprocess.
2. If the error persists, force a WAL checkpoint:
   `sqlite3 ~/.claude/toolforge.db "PRAGMA wal_checkpoint(FULL);"`.
3. As a last resort, list and kill stale Python processes holding the file.

**Why it's set up this way:** WAL plus 3-second busy timeout is the standard
SQLite-from-multiple-processes recipe. Longer timeouts would block the hot
path; shorter ones would error on legitimate contention.

### `~/.claude/toolforge.db` corrupted after a crash

**Cause:** A WAL truncation was interrupted (OS kill, power loss, disk full
during checkpoint). SQLite reports "database disk image is malformed" or
"file is encrypted or is not a database".

**Fix:**
1. Back up the corrupt file first:
   `mv ~/.claude/toolforge.db ~/.claude/toolforge.db.corrupt`.
2. Also move aside `~/.claude/toolforge.db-wal` and `~/.claude/toolforge.db-shm`
   if they exist.
3. Re-initialize: `python toolforge/bin/toolforge_db.py init`.
4. Historical install and rating data is lost. The corrupt file is preserved on
   disk if recovery is later possible via `sqlite3 ... .recover`.

**Why it's set up this way:** Schema is small and re-init is cheap. Attempting
in-place repair is fragile; a clean rebuild is the supported recovery path.

### Windows: PostToolUse counter file write fails with `sharing violation`

**Cause:** On Windows, parallel `open(path, "ab")` calls from concurrent
PostToolUse fires can collide with each other's open file handles, producing a
sharing violation. The hook has an 8-attempt linear backoff
(5ms, 10ms, 15ms, ..., 40ms) that masks this in the vast majority of cases.

**Fix:**
1. If the message surfaces in stderr only once or twice, it is transient.
   Re-run the operation. The next PostToolUse fire will succeed.
2. If it surfaces persistently (multiple times per session), the backoff is not
   keeping up with the parallelism. File an issue with your Windows version,
   the number of parallel agents in flight, and the exact error message.
3. The hook exits non-zero on persistent failure so Claude Code surfaces it in
   the transcript rather than silently dropping increments.

**Why it's set up this way:** Single-byte appends are atomic under O_APPEND on
POSIX, but Windows file-share semantics still allow collision on the open
itself. Backoff is preferred over `O_DENY_NONE` mode hackery.

### Demo: `/toolforge UI` returns fictional tools (pre-Phase-3 fix)

**Cause:** Legacy fallback JSON contains packages that no longer exist on npm
or have been renamed. The fallback was assembled from a snapshot that has
drifted.

**Fix:**
1. Pull the latest from main: `git pull origin main`. The Phase-3 fallback-fix
   landed there.
2. If you maintain a local fork or have edited the fallback yourself, re-run
   the fallback-fix procedure described in
   [CONTRIBUTING.md](./CONTRIBUTING.md#fallback-fix-procedure):
   validate each entry against npm, drop the dead ones, replace with
   currently-installable equivalents.
3. After editing, regenerate `fallback/manifest.sha256` (see entry 1).

**Why it's set up this way:** The fallback is a snapshot, not a registry.
Drift is expected. The fix is periodic curation, not real-time validation
(which would defeat the point of having a fallback).

### Hook timeout exceeded

**Cause:** The PostToolUse hook has a 5-second timeout, SessionEnd has 10
seconds (both defined in `plugin.json`). If the Python interpreter cold-start
plus the script body exceeds these, Claude Code kills the hook process.

**Fix:** Not user-fixable in v0.1. The timeouts are conservative for a stdlib-
only script: PostToolUse does one file append (microseconds), SessionEnd does
one file stat plus one SQLite read (milliseconds). If you are hitting timeouts:
- Check disk health. Slow disk under SSD trim or full-disk-encryption thrash
  can push tens of milliseconds into seconds.
- Check antivirus. Real-time scanners can add 1+ second per Python launch on
  Windows.
- Check Python startup. `python -c "pass"` should take well under a second on
  any modern machine.

**Why it's set up this way:** Conservative timeouts protect the user's
interactive responsiveness. Loosening them invites worse hangs.

### `/toolforge UI` does not show my installed plugins

**Cause:** `bin/toolforge_local_scan.py` shells out to `claude plugin list` and
`claude mcp list` to populate the installed-tools branch of the local-source
scan. Either the subprocess timed out (the per-call cap is
`CLAUDE_LIST_TIMEOUT_SECONDS = 5`), or the CLI returned no rows because it had
not finished warming up yet (cold start on Windows under antivirus inspection
can blow past 5 seconds on the first invocation of a session).

**Fix:**
1. Wait for `claude` to finish warming up (run `claude plugin list` once in a
   shell and confirm it returns), then run `/toolforge-rescan` to drop the
   stale 5-minute cache.
2. If the timeout is the real bottleneck on your machine, edit
   `CLAUDE_LIST_TIMEOUT_SECONDS` near the top of
   `bin/toolforge_local_scan.py` upward (10 to 15 seconds is reasonable for a
   slow Windows box). Re-run `/toolforge-rescan` after editing.
3. Confirm via `python toolforge/bin/toolforge_local_scan.py ui | python -m json.tool`
   that the `installed=True` entries appear in raw scanner output. If they
   appear there but not in `/toolforge UI`, the issue is downstream in the
   curator skill, not the scanner.

**Why it's set up this way:** A short subprocess timeout keeps the local-scan
budget bounded so a hung `claude` invocation cannot freeze the whole curator
pipeline. The trade is that genuinely slow `claude` cold starts will miss the
window once. See [ARCHITECTURE.md](./ARCHITECTURE.md) section 4.5 for the full
list of scanner caps.

### `/toolforge UI` shows tools from a repo I deleted

**Cause:** Local-scan results are cached at
`tempdir/toolforge_local_scan_<category>.json` for 5 minutes. If you deleted a
repo from `~/.claude/skills/`, removed a path from the `local_paths` array in
`~/.claude/toolforge-config.json`, or uninstalled a plugin via
`claude plugin uninstall`, the cache still references the prior state until it
expires.

**Fix:**
1. Run `/toolforge-rescan`. The slash command unlinks all five per-category
   cache files in a single sweep.
2. Or wait up to 5 minutes; the cache file's mtime gates expiry, so the next
   `/toolforge <category>` after the TTL elapses will rebuild from disk.
3. Verify the rebuild picked up the change with
   `python toolforge/bin/toolforge_local_scan.py <category> | python -m json.tool`.

**Why it's set up this way:** A 5-minute TTL is long enough to avoid
back-to-back filesystem walks in the same session, short enough that stale
entries do not linger across normal work patterns. `/toolforge-rescan` is the
escape hatch for when you need a forced refresh.

### Local-scan returns nothing for a category despite having relevant tools locally

**Cause:** The categorization heuristic in `bin/toolforge_local_scan.py` did
not find enough keyword matches in the tool's `name + description` to clear
the drop threshold of `0.3`. The scanner uses a fixed per-category keyword set
(`CATEGORY_KEYWORDS`); a skill named `helper-x` with description `"general
utility"` will not match any category.

**Fix:**
1. Inspect the raw scan to confirm the entry is being read at all:
   `python toolforge/bin/toolforge_local_scan.py <category> | python -m json.tool`.
   If the entry is absent entirely, the issue is depth, file count, or symlink
   walk, not categorization.
2. Either edit `CATEGORY_KEYWORDS` in the scanner to add the keyword you
   expect (e.g., add `"helper"` to the relevant set), then run
   `/toolforge-rescan`. The keyword sets are documented verbatim in
   [ARCHITECTURE.md](./ARCHITECTURE.md) section 4.3.
3. Or rename the local SKILL.md to include a category-matching keyword in its
   `name` field or first description line. The scanner reads up to 4 KiB; the
   keyword needs to land in that window.

**Why it's set up this way:** A keyword heuristic with a hard drop threshold
is cheap, predictable, and easy to debug. ML-based categorization would mask
this exact class of problem behind learned weights.

### `~/.claude/toolforge-config.json` is unreadable

**Cause:** Either the file is missing, contains invalid JSON, or lacks the
expected top-level `local_paths` array. The scanner refuses to guess at the
contents of a malformed config rather than silently substituting one value for
another.

**Fix:**
1. Validate the JSON: `python -m json.tool ~/.claude/toolforge-config.json`.
   The first parse error pinpoints the offending line.
2. Confirm the shape matches:
   ```json
   {
     "local_paths": [
       "/absolute/path/one",
       "/absolute/path/two"
     ]
   }
   ```
3. If you want to disable user-configured local paths entirely, delete the
   file; the scanner falls back to the default scan roots
   (`~/.claude/skills`, `~/.claude/agents`, `<cwd>/.claude/*`,
   `claude plugin list`, `claude mcp list`) and prints a one-line stderr
   warning naming the missing or unreadable config file.

**Why it's set up this way:** Silent fallback to defaults plus a stderr
warning is the right default for an optional config file: the plugin keeps
working, and the user sees the warning when they run `/toolforge` next.
Refusing to start would be worse. See
[CONTRIBUTING.md](./CONTRIBUTING.md) if you want to extend the config schema.

### `toolforge_install.py` exits 3 (`audit log dropped after success`)

**Cause:** The install command itself succeeded (subprocess exit 0), but the
post-install `_safe_log(approved=True)` call to write the audit row to SQLite
failed. Common reasons: DB lock held by another process, disk full, permission
issue on `~/.claude/`.

**Fix:**
1. The install is good. The package or plugin is in place; nothing was rolled
   back. Verify with the tool's own CLI or `claude mcp list`.
2. The audit row is missing, which means `/toolforge-rate <n>` will not find
   this install as the "last installed tool". Either:
   - Fix the underlying DB issue (see SQLite entries above) and manually
     re-rate with `python toolforge/bin/toolforge_db.py log_rating <tool> <n>`.
   - Or accept the loss and rate the next install instead.
3. Check stderr for the diagnostic from `_safe_log`; it prints
   `toolforge audit: log dropped for <name>: <error>` on failure.

**Why it's set up this way:** The split between "install succeeded" and "audit
logged" is deliberate. We do not want to rollback a successful install because
SQLite was momentarily unavailable; we do want to flag the gap loudly so the
user knows the rating loop is broken for that install.
