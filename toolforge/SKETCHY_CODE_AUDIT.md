# ToolForge Sketchy Code Audit

Audit findings across `bin/`, `hooks/`, `webui/`. Generated 2026-05-27 by parallel multi-agent review (silent-failure-hunter ×3, code-reviewer, comment-analyzer, cavecrew-investigator).

This document is the canonical list of **known issues, drift, and future-risk spots**. Each finding has a stable anchor — inline `# WARN: see SKETCHY_CODE_AUDIT.md#sX-Y` comments in source point here.

Severity scale:
- **CRITICAL** — security boundary failure, data loss, RCE, injection
- **HIGH** — silent failure that masks a real bug, supply-chain risk, race on hot path
- **MED** — quality/drift/race in rare path
- **LOW** — style, future-proofing, comment rot

---

## Table of contents

- [§1 Critical findings](#s1)
- [§2 High findings](#s2)
- [§3 Medium findings — duplication & drift](#s3)
- [§4 Medium findings — error handling & races](#s4)
- [§5 Low findings — comment rot & stale references](#s5)
- [§6 Comment-rot vs ARCHITECTURE.md](#s6)
- [§7 Dead code](#s7)
- [§8 Positive findings (don't refactor away)](#s8)
- [§9 Recommended fix order](#s9)

---

<a id="s1"></a>
## §1 Critical findings

### §1.1 Format-string injection in security handoff
- **File:** `bin/toolforge_security_handoff.py:41`
- **Smell:** `template.format(source_url=..., tool_name=...)` on attacker-controlled strings. `tool_name` is only length-validated, not charset-validated. `source_url` only has its hostname allow-listed; path is free-form.
- **Attack:** Adversary names a tool `{0.__class__.__mro__[1].__subclasses__}` (Python format-string attribute traversal) or buries `{}` chars in `source_url` path. Either crashes with `KeyError` exposing a traceback, or worse — leaks internals via Python's format-string DSL.
- **Fix:** Replace `.format()` with `string.Template(template).safe_substitute(source_url=..., tool_name=...)`, or use explicit `.replace("{source_url}", ...)`. Also enforce `TOOL_NAME_RE = ^[a-z0-9._@/-]{1,80}$` on `tool_name` and a path-charset gate on `source_url`.

### §1.2 Local XSS via `.html` in ALLOWED_OPEN_EXTS
- **File:** `webui/server.py:60` (ALLOWED_OPEN_EXTS), `webui/server.py:340-345` (/api/open route), `webui/static/index.html:8,110` (external CDN deps)
- **Smell:** `/api/open` calls `os.startfile` / `xdg-open` after path-validation against `SAFE_ROOTS`. `ALLOWED_OPEN_EXTS` includes `.html`. A local attacker who can plant a file at a path like `~/.claude/skills/toolforge-<slug>/SKILL.md.html` (slug is derived from the flow trigger, which the user types — so this is mostly self-attack, but supply-chain skills that ship `.html` would also detonate) gets arbitrary JS execution in the user's default browser with same-origin access to any localhost service the user has running.
- **Combined with:** `index.html` loads `cdn.jsdelivr.net/npm/drawflow@0.0.59` and Google Fonts via `styles.css:7` — CDN compromise → arbitrary JS in the WebUI itself with same-origin access to `/api/*` (only blocked by the CSRF token, which a same-origin script reads from the page).
- **Fix:** Drop `.html` from `ALLOWED_OPEN_EXTS`. Vendor drawflow into `webui/static/vendor/`. Drop the Google Fonts `@import` or vendor those too.

### §1.3 Silent error swallows in frontend
- **File:** `webui/static/app.js:608` (loadSavedFlows), `webui/static/app.js:674` (addConnection)
- **Smell:** `} catch (e) { /* silent */ }` and `try { state.editor.addConnection(...) } catch { /* ignore dup */ }`. The first hides flow-list fetch failures (UI just shows "no saved flows" even when the fetch died). The second hides ALL drawflow exceptions, not just dup-edge — corrupted flows silently drop edges with no user feedback.
- **Fix:** Emit a toast / status banner on both. For `addConnection`, narrow the catch to the specific dup-edge exception drawflow throws.

---

<a id="s2"></a>
## §2 High findings

### §2.1 Silent SQLite failure masked from UI
- **File:** `webui/inventory.py:439`
- **Smell:** `bulk = toolforge_db.get_rating_stats_bulk(names)` returns a `_error` key on DB failure, but the loop at line 453 still assigns `rating` defaults and never surfaces the error to the inventory caller. The UI shows "prior only" ratings indistinguishably from "DB locked, ratings unavailable".
- **Fix:** When `bulk` contains `_error`, push the error into `inventory.warnings` so the UI banner shows "Rating data unavailable: <reason>".

### §2.2 Non-atomic skill export
- **File:** `webui/exporter.py:208-228`
- **Smell:** `export_flow()` writes `SKILL.md` then `flow.json` to the same skill directory non-atomically. If `flow.json` write fails (disk full, AV lock), the directory contains an orphan SKILL.md that subsequent loads will try to parse.
- **Fix:** Write both files to a temp subdir, then `os.replace` the dir into place. Alternatively, write `flow.json` first (the failure-resilient sentinel) and `SKILL.md` second.

### §2.3 Silent flow-file JSON corruption
- **File:** `webui/server.py:217-219, 228`
- **Smell:** `except (OSError, json.JSONDecodeError): continue` on the flow listing endpoint. Corrupt flow file vanishes from the listing with zero user feedback. The `_scan_plugins` codepath in `inventory.py` already does this correctly by emitting a `_warning` — port that pattern here.
- **Fix:** On parse failure, include a warning entry in the response describing which file is broken and why.

### §2.4 No subprocess timeout on install
- **File:** `bin/toolforge_install.py:287, 397`
- **Smell:** `subprocess.run(..., timeout=None)`. An adversarial postinstall script hangs indefinitely; user's only recourse is killing the parent process.
- **Fix:** `timeout=300` (5 min) + handle `TimeoutExpired` by killing the process group and logging an audit drop.

### §2.5 sqlite3.Error not caught in usage-detector persist loop
- **File:** `bin/toolforge_usage_detector.py:219`
- **Smell:** `persist_to_db` catches `(ValueError, OSError)` but not `sqlite3.Error`. A single transient DB error mid-loop crashes the function and silently drops all remaining rows.
- **Fix:** Catch `sqlite3.Error` too. Return `(written, failed)` tuple. Log failures with row context.

### §2.6 Insufficient retry budget on Windows counter writes
- **File:** `hooks/post-tool-use-counter.py:88-95`
- **Smell:** Linear backoff 5+10+15+...+40 = 180ms total over 8 retries. On a hot parallel session (many concurrent edits + AV scanner holding the handle), 180ms may not be enough — increment is lost, SessionEnd Likert prompt never fires.
- **Fix:** Switch to `os.open(path, O_APPEND|O_CREAT|O_WRONLY)` + single-byte `os.write()` (no Python file-object lock contention). Or bump retries to 16 with exponential backoff capped at 1s.

### §2.7 TOCTOU on counter stat at session end
- **File:** `hooks/session-end-likert.py:99`
- **Smell:** `path.exists()` followed by `path.stat()`. Parallel PostToolUse could append between the two, or stale-prune in another hook could unlink. Stat raises and the except branch emits a misleading "counter unreadable".
- **Fix:** Collapse to a single `try: count = path.stat().st_size; except FileNotFoundError: count = 0`.

### §2.8 Active counter could be pruned mid-session
- **File:** `hooks/post-tool-use-counter.py:36-48`
- **Smell:** `_prune_stale` iterates the counter directory looking for files >7d old. If system clock skew or a fresh file with old mtime exists, this could unlink the LIVE counter mid-session.
- **Fix:** Pass the active counter path in and skip it explicitly: `if p.samefile(active_path): continue`.

### §2.9 Stale-prune runs on every PostToolUse
- **File:** `hooks/post-tool-use-counter.py:84`
- **Smell:** `_prune_stale` glob + stat fires on every tool call. 100+ calls/session + slow disk → risk of 5s hook timeout.
- **Fix:** Gate on `random.random() < 0.01` or skip if a sentinel mtime on tmpdir says we already pruned in the last hour.

### §2.10 Session ID env var fallback may not match Claude Code
- **File:** `hooks/post-tool-use-counter.py:27-33`, `hooks/session-end-likert.py` (mirror)
- **Smell:** Both hooks fall back to `os.environ.get("CLAUDE_SESSION_ID")` if stdin doesn't contain `session_id`. The actual Claude Code env var name needs verification — if it's `CLAUDE_CODE_SESSION_ID` or otherwise different, the fallback never fires.
- **Fix:** Grep Claude Code docs for the canonical env var name; document the answer here.

### §2.11 Unbounded RAM on static file read
- **File:** `webui/server.py:161`
- **Smell:** `target.read_bytes()` loads the entire static asset into RAM with no size cap. A future >100MB asset OOMs the server thread.
- **Fix:** Stream via `wfile.write()` in chunks, or cap with `target.stat().st_size > MAX_STATIC_BYTES` check.

---

<a id="s3"></a>
## §3 Medium findings — duplication & drift

### §3.1 `DECAY_HALFLIFE_DAYS = 75.0` declared 3×
- **Files:** `bin/toolforge_db.py:26`, `bin/toolforge_local_scan.py:52`, `webui/inventory.py:53`
- **Smell:** README and ARCHITECTURE both promise "lockstep" decay across rating and recency. Three copies means one bump misses two readers.
- **Fix:** Pick `toolforge_db.py` as canonical; both other files already `sys.path.insert` the `bin/` dir, so `from toolforge_db import DECAY_HALFLIFE_DAYS` works.

### §3.2 `CATEGORY_KEYWORDS` duplicated byte-identical
- **Files:** `webui/inventory.py:60` (called `CURATOR_KEYWORDS`), `bin/toolforge_local_scan.py:63` (called `CATEGORY_KEYWORDS`)
- **Smell:** Same five-category keyword dict, two names, two source-of-truth copies. Comment at `inventory.py:58` admits the port.
- **Fix:** Import from `toolforge_local_scan`. Pick one name (`CATEGORY_KEYWORDS`).

### §3.3 `_recency_norm_from_path` duplicated with drift
- **Files:** `webui/inventory.py:540`, `bin/toolforge_local_scan.py:205`
- **Smell:** Different signatures (`Path` vs `Optional[str]`) and divergent None-handling (`inventory` returns 1.0 on no-path, `local_scan` requires the arg).
- **Fix:** Extract to shared helper module, or import the bin version into inventory and adapt callers.

### §3.4 `_parse_frontmatter` duplicated with divergent semantics
- **Files:** `webui/inventory.py:181`, `bin/toolforge_local_scan.py:172`
- **Smell:** Same function name, different parsers (regex+line-walk vs block-find+regex). They disagree on multi-line values and missing closing `---`.
- **Fix:** Pick one; import the other side.

### §3.5 Three different `_normalize` implementations
- **Files:** `bin/toolforge_db.py:54` (`_normalize`), `bin/toolforge_local_scan.py:152` (`_normalize_name`), `bin/toolforge_usage_detector.py:63` (`_normalize_name`)
- **Smell:** Three impls of the same concept that disagree on `"foo bar"`, `"foo:bar"`, `"foo;bar"`. `db` strips+lowers, `local_scan` regex-subs to `-`, `usage_detector` char-loops with colon→slash.
- **Fix:** Pick `usage_detector`'s impl as canonical (it documents the colon rule). Rename the others to distinct names if the API split is genuinely needed, otherwise import one.

### §3.6 `MAX_PORT_SCAN` drift between server and launcher
- **Files:** `webui/server.py:50` (`MAX_PORT_SCAN = 50` starting at `DEFAULT_PORT = 7321`), `webui/launch.py:21` (`DEFAULT_PORT_RANGE = range(7321, 7371)`)
- **Smell:** Numerically aligned today; one bump desyncs.
- **Fix:** Define the range constant in `server.py`, import into `launch.py`.

### §3.7 Module-global mutable state in `toolforge_db.py`
- **File:** `bin/toolforge_db.py:36` (`_session_count_had_error`)
- **Smell:** Module-level flag mutated by `_current_session_count` and reset inside `status()`. Re-entrancy unsafe under concurrent imports (webui/inventory imports this).
- **Fix:** Return `(count, had_error)` tuple from `_current_session_count`. Drop the global.

### §3.8 Non-deterministic UI keys
- **File:** `webui/inventory.py:329`
- **Smell:** `abs(hash(proj_path)) % 10**8` — Python `hash()` is salted by `PYTHONHASHSEED`, so UI keys shift across server restarts. Frontend state tied to these keys breaks.
- **Fix:** `hashlib.sha1(proj_path.encode()).hexdigest()[:12]`.

---

<a id="s4"></a>
## §4 Medium findings — error handling & races

### §4.1 Config file corruption silently masked
- **File:** `bin/toolforge_local_scan.py:113`
- **Smell:** `_load_config` returns `{}` on JSONDecodeError without quarantining. The cache code (`_load_cache`) DOES quarantine — inconsistent. Tampered config invisible.
- **Fix:** Match the cache pattern: rename bad config to `.corrupt.<ts>` and stderr-warn.

### §4.2 Unreadable subdir vanishes silently
- **File:** `bin/toolforge_local_scan.py:326`
- **Smell:** `except OSError: continue` on subdir walk. Permission glitches make skill auditing think the dir is empty.
- **Fix:** Stderr-warn the path and errno before continuing.

### §4.3 Off-by-one on file-count cap
- **File:** `bin/toolforge_local_scan.py:312-314`
- **Smell:** `count += 1` runs before `count >= MAX_FILES_SCANNED` check. Cap is advertised as N but visits N+1.
- **Fix:** Increment after the check, or document the +1.

### §4.4 Non-atomic cache writes
- **Files:** `bin/toolforge_local_scan.py:524`, `bin/toolforge_usage_detector.py:268`
- **Smell:** `path.write_text()` non-atomic — crash mid-write yields partial file. Recovery relies on the quarantine path on next read.
- **Fix:** Write to `<path>.tmp` then `os.replace`. Standard pattern.

### §4.5 Race on `/api/export` of same trigger
- **File:** `webui/exporter.py:225`
- **Smell:** `skill_dir.mkdir(parents=True, exist_ok=True)` then `write_text`. Two simultaneous clients hitting `/api/export` with the same trigger both pass mkdir, then both write — last-writer-wins silently.
- **Fix:** Acquire a per-trigger lock (file-based or in-memory), or write to a temp dir and `os.replace` atomically.

### §4.6 Blank/symbol-only trigger collides on "untitled-flow"
- **File:** `webui/exporter.py:266`
- **Smell:** If `trigger` is empty or all-special-chars, `slugify` returns `"untitled-flow"`. Any subsequent `delete_exported_flow("")` nukes the previous user's `toolforge-untitled-flow` skill.
- **Fix:** Reject blank/all-symbol triggers in the API handler.

### §4.7 Format-string vs server response shape
- **File:** `webui/static/app.js:380`
- **Smell:** `r.json().catch(() => ({}))` swallows parse errors. When the server returns an HTML error page, `j` becomes `{}` and the user sees `Preview error: undefined`.
- **Fix:** On parse failure, surface `r.status` + `r.statusText` + a snippet of the body.

### §4.8 Malformed drop payload silently dies
- **File:** `webui/static/app.js:200`
- **Smell:** `JSON.parse(raw)` on drop without try/catch. Extensions or other apps can inject malformed `application/x-toolforge-tool` payloads (rare, but possible). Drop silently fails.
- **Fix:** try/catch + user-visible toast.

### §4.9 Stale-prune logging gap in usage detector
- **File:** `bin/toolforge_usage_detector.py:158, 220`
- **Smell:** Bare `except OSError: pass` mid-read. Partial scans look complete from the caller's POV. Skip count not propagated.
- **Fix:** Stderr-log on swallow. Return `(written, skipped)` tuple. CLI prints both.

### §4.10 `claude plugin list` parser brittle on header lines
- **File:** `bin/toolforge_local_scan.py:393-394`
- **Smell:** `line.startswith` heuristic for header detection. Plugins legitimately named starting with "Plugin"/"Name"/"NAME" are silently dropped.
- **Fix:** Use `claude plugin list --json` if available, or detect the header row by a more specific pattern.

### §4.11 Empty hash file → uncaught IndexError
- **File:** `bin/toolforge_verify_fallback.py:71`
- **Smell:** `hash_path.read_text().strip().split()[0]` raises IndexError on empty file. Bypasses the script's intended exit 3 contract.
- **Fix:** `tokens = ... .split(); if not tokens: return 3`.

### §4.12 `--days N` parse failure silently defaults
- **File:** `bin/toolforge_dumb_scanner.py:108`
- **Smell:** `except ValueError: pass` swallows bad `--days` input. User intent lost.
- **Fix:** Stderr-warn + exit 2. (Or: delete the file entirely; see §7.)

---

<a id="s5"></a>
## §5 Low findings — comment rot & stale references

### §5.1 Stale `PATTERNS.md` reference
- **File:** `bin/toolforge_install.py:529`
- **Smell:** Comment references "PATTERNS.md §7"; no such file exists in the repo.
- **Fix:** Either restore the doc or point to `ARCHITECTURE.md §7` (the actual reference).

### §5.2 Sprint-tag prefixes (`M5:`, `M6:`)
- **Files:** `bin/toolforge_db.py:468`, `bin/toolforge_local_scan.py:612`
- **Smell:** Comments lead with sprint identifiers nobody else will ever decode.
- **Fix:** Drop the prefix; keep the explanation.

### §5.3 Stale env-var docstring in `_self_test`
- **File:** `bin/toolforge_db.py:531`
- **Smell:** Docstring says "Override via TOOLFORGE_DB env var" but no `TOOLFORGE_DB` lookup exists in the file.
- **Fix:** Either implement the override or delete the line.

### §5.4 Stale v0.1/v0.2 deferral note
- **File:** `bin/toolforge_install.py:149-150`
- **Smell:** Comment says option-passing is deferred to v0.2. Repo is on v0.2 docs branch already. Either deliver or restate.

### §5.5 Stale "earlier drafts" paragraph
- **File:** `bin/toolforge_install.py:18-24`
- **Smell:** Module docstring describes a feature that was reverted. Design-history noise.
- **Fix:** Trim to current-state.

### §5.6 Lying docstring for `_dedupe`
- **File:** `webui/inventory.py:475`
- **Smell:** Docstring says "Dedup by absolute path", actual key includes `source` fallback.
- **Fix:** Align doc to behavior or vice versa.

### §5.7 Subprocess list in module docstring missing `git log`
- **File:** `bin/toolforge_local_scan.py:28`
- **Smell:** Module docstring lists only `claude plugin list` + `claude mcp list`; misses `git log -1 --format=%ct` invoked at line 222-231. ARCHITECTURE.md §5.4 lists all three.
- **Fix:** Add `git log` to the docstring.

### §5.8 Lying `get_rating_stats_bulk` complexity claim
- **File:** `bin/toolforge_db.py:206`
- **Smell:** "O(1) round trips, not O(N)" misleads a skimmer to think scoring is O(1) total. Round-trips are O(1); the SQL itself is O(N).
- **Fix:** "Single round-trip — N is the SQL IN list size, not the number of network hops."

---

<a id="s6"></a>
## §6 Comment-rot vs ARCHITECTURE.md

### §6.1 Timeout constants drift
- **File:** `bin/toolforge_local_scan.py:57-58`
- **Code says:** `SUBPROCESS_TIMEOUT_SECONDS = 4.0`, `CLAUDE_LIST_TIMEOUT_SECONDS = 10.0`
- **ARCH §4.5 + §5.4 say:** `CLAUDE_LIST_TIMEOUT_SECONDS = 5`, `GIT_LOG_TIMEOUT_SECONDS = 2`
- **Reality:** Code never defines `GIT_LOG_TIMEOUT_SECONDS`; `SUBPROCESS_TIMEOUT_SECONDS` is what `git log` actually uses.
- **Pick a direction:** Either shrink to match ARCH (5/2) or bump ARCH to match code (10/4). Doc claims a stricter budget than code enforces — the worst direction for a security boundary.

---

<a id="s7"></a>
## §7 Dead code

### §7.1 `toolforge_dumb_scanner.py`
- **File:** `bin/toolforge_dumb_scanner.py`
- **Status:** Zero imports across `bin/`, `webui/`, `hooks/`, `tests/`; only doc-referenced. Superseded by `toolforge_usage_detector.py`.
- **Fix:** Delete the file. Scrub the two doc references in `docs/v0.2/{01_END_PRODUCT,02_EXECUTION_PLAN}.md`.

### §7.2 `DISCOVERY_REPOS` module-level alias
- **File:** `webui/inventory.py:162`
- **Status:** `DISCOVERY_REPOS = _resolved_discovery_repos()` assigned eagerly at import. Zero external readers. Comment claims "lazy" but the binding is eager.
- **Fix:** Delete the constant + comment. Callers already use the resolver directly.

---

<a id="s8"></a>
## §8 Positive findings (don't refactor away)

- `bin/toolforge_install.py:1-25` — module docstring is exemplary: states purpose, threat model, validation strategy, trust boundary. Use as template for other files. (One stale paragraph to trim — §5.5.)
- `bin/toolforge_validate_url.py:43-52` — `_has_forbidden_bytes` names the attack class (parser-differential) inline. Great inline-WHY example.
- `bin/toolforge_db.py:459-464` — `_shrunk_score` docstring is exactly the right size and cross-references the curator skill.
- `hooks/post-tool-use-counter.py:88-104` — Windows sharing-violation retry loop is correctly motivated by Windows-specific behavior.
- `bin/toolforge_security_handoff.py:33-35` — explains WHY risk patterns live in an external file (security_reminder_hook would block edits). Preempts a "let's inline this" refactor.
- `bin/toolforge_verify_fallback.py:76,114` — `secrets.compare_digest` for constant-time hash check. Correct.
- All subprocess calls use `shell=False`. Correct.
- Schema migration via `PRAGMA user_version` (`bin/toolforge_db.py:102-132`). Correct.

---

<a id="s9"></a>
## §9 Recommended fix order

Highest leverage first. Each unblocks no others — fixes can be parallelized.

1. **§1.1** — Replace `.format()` in `toolforge_security_handoff.py:41` with `string.Template.safe_substitute`. Add `TOOL_NAME_RE` validation.
2. **§1.2** — Drop `.html` from `ALLOWED_OPEN_EXTS`. Vendor `drawflow` + Google Fonts into `webui/static/vendor/`.
3. **§1.3** — Surface errors in `app.js:608, 674` via toast.
4. **§2.4** — Add `timeout=300` + `TimeoutExpired` handler in `toolforge_install.py:287, 397`.
5. **§2.5** — Catch `sqlite3.Error` in `toolforge_usage_detector.py:219`. Return `(written, failed)`.
6. **§6.1** — Reconcile timeout constants with ARCHITECTURE.md.
7. **§3.1, §3.2, §3.3, §3.4, §3.5** — Collapse the five duplicated constants/functions into single sources.
8. **§2.1** — Surface DB-locked rating error from `inventory.py:439` into UI banner.
9. **§2.2** — Atomic skill export in `exporter.py:208-228`.
10. **§7.1, §7.2** — Delete dead code.
11. Remaining MEDs in order listed.
12. LOWs as cleanup passes.

---

## Inventory of files audited

```
bin/toolforge_db.py                  766 lines  (CRITICAL audited)
bin/toolforge_install.py             766 lines  (CRITICAL audited)
bin/toolforge_local_scan.py          671 lines  (CRITICAL audited)
bin/toolforge_usage_detector.py      495 lines  (HIGH audited)
bin/toolforge_validate_url.py        101 lines  (HIGH audited)
bin/toolforge_rate.py                 ~50 lines (LOW audited)
bin/toolforge_verify_fallback.py     135 lines  (NEW — uncommitted)
bin/toolforge_security_handoff.py     70 lines  (NEW — uncommitted, CRITICAL)
bin/toolforge_dumb_scanner.py        139 lines  (DEAD — see §7.1)
hooks/post-tool-use-counter.py       ~120 lines (HIGH audited)
hooks/session-end-likert.py          138 lines  (HIGH audited)
webui/server.py                      388 lines  (CRITICAL audited)
webui/inventory.py                   743 lines  (HIGH audited)
webui/exporter.py                    288 lines  (HIGH audited)
webui/launch.py                       ~60 lines (LOW audited)
webui/static/app.js                  ~700 lines (HIGH audited)
webui/static/index.html              ~140 lines (CRITICAL — CDN deps)
webui/static/styles.css                       (LOW — Google Fonts import)
webui/static/sidebar-collapse.js              (clean)
webui/static/sidebar-resize.js                (clean)
```

Total: 20 source files, ~5500 lines reviewed.

---

*This audit was generated by parallel-agent review on 2026-05-27. Re-run agents and update findings before each release.*
