# ToolForge Fix Playbook

Companion to `SKETCHY_CODE_AUDIT.md`. Each ticket (`F##`) is a discrete fix sized for one agent. Tickets cross-reference audit anchors (`§X-Y`).

Order is roughly priority-descending. Tickets are file-disjoint within a wave.

---

## §A — CRITICAL fixes (Wave 1)

### F01 — Format-string injection in security handoff (§1-1)
**File:** `bin/toolforge_security_handoff.py:41`
**Fix:**
- Replace `template.format(source_url=..., tool_name=...)` with `string.Template(template).safe_substitute(source_url=..., tool_name=...)`.
- Import `string` at top.
- Verify the template file uses `${source_url}` / `${tool_name}` (Template syntax). If it uses `{source_url}` / `{tool_name}` (format syntax), rewrite the template file too.
- Add charset validation on `tool_name` before substitution:
  ```python
  TOOL_NAME_RE = re.compile(r"^[a-z0-9._@/\-]{1,80}$")
  if not TOOL_NAME_RE.match(tool_name): raise ValueError(...)
  ```
- Add `source_url` path-charset gate: reject `{`, `}` and control bytes before substitution.

### F02 — Drop `.html` from `ALLOWED_OPEN_EXTS` (§1-2)
**File:** `webui/server.py:60`
**Fix:** Remove `.html` from the `ALLOWED_OPEN_EXTS` set. Keep `.md`, `.json`, `.txt`. Add a comment: `# .html intentionally omitted — OS handler executes JS, see SKETCHY_CODE_AUDIT.md#s1-2`.

### F03 — Vendor `drawflow` and Google Fonts (§1-2 part B)
**Files:** `webui/static/index.html:8,110`, `webui/static/styles.css:7`
**Fix:**
- Create `webui/static/vendor/` directory.
- Download `drawflow@0.0.59` (`drawflow.min.css` + `drawflow.min.js`) into `webui/static/vendor/`. Use `urllib.request` from a setup script or commit them directly. Reference URL: `https://cdn.jsdelivr.net/npm/drawflow@0.0.59/dist/drawflow.min.{js,css}`.
- Rewrite `index.html` to load from `/static/vendor/drawflow.min.{js,css}` instead of CDN.
- Remove the `@import url('https://fonts.googleapis.com/...')` line from `styles.css`. Replace with a system-font stack: `font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;`.

### F04 — Silent catch in `loadSavedFlows` (§1-3)
**File:** `webui/static/app.js:608`
**Fix:** Replace `} catch (e) { /* silent */ }` with a toast/banner: `} catch (e) { showToast('Failed to load saved flows: ' + e.message, 'error'); console.error(e); }`. If no `showToast` helper exists, add one (single-function append-to-DOM banner with auto-dismiss).

### F05 — Silent catch in `addConnection` (§1-3 part B)
**File:** `webui/static/app.js:674`
**Fix:** Narrow the catch to drawflow's specific dup-edge exception. Pattern:
```js
try {
  state.editor.addConnection(...);
} catch (e) {
  const isDup = /already exists|duplicate/i.test(e?.message || '');
  if (!isDup) { showToast('Edge add failed: ' + e.message, 'warn'); console.error(e); }
}
```

---

## §B — HIGH fixes (Waves 2-3)

### F06 — Subprocess timeout on install (§2-4)
**File:** `bin/toolforge_install.py:287, 397`
**Fix:** At each `subprocess.run(...)` call inside `install()` and `install_batch()`:
- Add `timeout=INSTALL_TIMEOUT_SECONDS` (define `INSTALL_TIMEOUT_SECONDS = 300` at module top).
- Wrap with `try/except subprocess.TimeoutExpired as e: ...` that kills the process group, logs an audit drop via `_safe_log(...)`, and returns exit code 4 with stderr message naming the timeout.

### F07 — Catch `sqlite3.Error` in usage detector persist (§2-5)
**File:** `bin/toolforge_usage_detector.py:219`
**Fix:** Change `except (ValueError, OSError) as exc:` to `except (ValueError, OSError, sqlite3.Error) as exc:`. Track failed rows in a counter; return `(written, failed)` from `persist_to_db`. Update caller in `main()` to print both counts.

### F08 — Surface DB-locked error to UI (§2-1)
**File:** `webui/inventory.py:439`
**Fix:** After the `bulk = toolforge_db.get_rating_stats_bulk(names)` call, check for `_error` key. If present, append a dict to `warnings` list:
```python
if isinstance(bulk, dict) and "_error" in bulk:
    warnings.append({"type": "rating_unavailable", "detail": bulk["_error"]})
```
Ensure `warnings` is returned from `build_inventory` and surfaced in the API response.

### F09 — Silent flow JSON corruption (§2-3)
**File:** `webui/server.py:217-219, 228`
**Fix:** Replace `except (OSError, json.JSONDecodeError): continue` with a block that captures the error path + reason into a `_warnings` list on the response:
```python
except (OSError, json.JSONDecodeError) as exc:
    response_warnings.append({"file": fp.name, "error": str(exc)})
    continue
```
Surface `response_warnings` in the listing endpoint response. For the bare `json.loads` at line 228, wrap with `try/except json.JSONDecodeError` returning 422 with `{"error": "flow file corrupted", "file": ...}`.

### F10 — Atomic skill export (§2-2)
**File:** `webui/exporter.py:208-228`
**Fix:** In `export_flow()`:
1. Write to `<skill_dir>.tmp` instead of `skill_dir`.
2. Write `flow.json` first, then `SKILL.md` (so partial-write leaves the failure-marker file).
3. `os.replace(tmp_dir, skill_dir)` to commit atomically.
4. On any exception during temp write, `shutil.rmtree(tmp_dir, ignore_errors=True)` and re-raise.

### F11 — Atomic cache writes (§4-4 × 2 sites)
**Files:** `bin/toolforge_local_scan.py:524`, `bin/toolforge_usage_detector.py:268`
**Fix:** Replace `path.write_text(json.dumps(...))` with the standard pattern:
```python
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(...), encoding="utf-8")
os.replace(tmp, path)
```
Identical change at both sites.

### F12 — Bump counter retry budget (§2-6)
**File:** `hooks/post-tool-use-counter.py:88-95`
**Fix:** Replace the Python-`open("ab")` retry loop with a single-syscall append:
```python
fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
try:
    os.write(fd, b".")
finally:
    os.close(fd)
```
Wrap in 4 retries with 25ms exponential backoff (`time.sleep(0.025 * (2**attempt))`) for residual Windows sharing-violation cases.

### F13 — TOCTOU on session-end counter stat (§2-7)
**File:** `hooks/session-end-likert.py:99`
**Fix:** Replace `if path.exists(): count = path.stat().st_size` with:
```python
try:
    count = path.stat().st_size
except FileNotFoundError:
    count = 0
```
Remove the redundant `exists()` check.

### F14 — Prune-stale could unlink active counter (§2-8)
**File:** `hooks/post-tool-use-counter.py:36-48`
**Fix:** Add `active_path` parameter to `_prune_stale(counter_dir, active_path)`. Skip via:
```python
for p in counter_dir.glob("toolforge_session_*.count"):
    try:
        if p.samefile(active_path):
            continue
    except OSError:
        pass
    # rest of stale check
```

### F15 — Gate stale-prune behind sampling (§2-9)
**File:** `hooks/post-tool-use-counter.py:84`
**Fix:** At the `_prune_stale` call site in `main()`, gate:
```python
import random
if random.random() < 0.01:
    _prune_stale(path.parent, path)
```
Constant: `_PRUNE_SAMPLE_RATE = 0.01` at module top.

### F16 — Stream + cap static file reads (§2-11)
**File:** `webui/server.py:161`
**Fix:** Add `MAX_STATIC_BYTES = 20 * 1024 * 1024` constant. Before `target.read_bytes()`, check `target.stat().st_size > MAX_STATIC_BYTES` → 413 response. Stream in 64KB chunks:
```python
with target.open("rb") as fh:
    while chunk := fh.read(65536):
        self.wfile.write(chunk)
```

---

## §C — MED fixes — duplication & drift (Wave 3-4)

### F17 — Extract `DECAY_HALFLIFE_DAYS` to canonical source (§3-1)
**Files:** Define in `bin/toolforge_db.py` (keep). Import in `bin/toolforge_local_scan.py:52` and `webui/inventory.py:53`.
**Fix:** In both consumer files:
```python
from toolforge_db import DECAY_HALFLIFE_DAYS
```
(sys.path.insert(0, str(bin_dir)) is already in place for `inventory.py`; `local_scan.py` shares the `bin/` dir directly.)
Delete the duplicate `DECAY_HALFLIFE_DAYS = 75.0` lines in those two files.

### F18 — Extract `CATEGORY_KEYWORDS` to canonical source (§3-2)
**Files:** Keep in `bin/toolforge_local_scan.py:63`. Import in `webui/inventory.py:60`.
**Fix:** In `inventory.py`, replace the local `CURATOR_KEYWORDS = {...}` dict with:
```python
from toolforge_local_scan import CATEGORY_KEYWORDS as CURATOR_KEYWORDS
```
Or rename usages to `CATEGORY_KEYWORDS` and drop the alias.

### F19 — Extract `_recency_norm_from_path` (§3-3)
**Files:** Keep in `bin/toolforge_local_scan.py:205`. Update `webui/inventory.py:540` to import.
**Fix:** In `inventory.py`:
```python
from toolforge_local_scan import _recency_norm_from_path
```
Delete the duplicate function. Audit call sites for signature compatibility (`Path` vs `Optional[str]`); coerce at call site if needed.

### F20 — Extract `_parse_frontmatter` (§3-4)
**Files:** Keep one impl as canonical (recommend `local_scan.py`'s — simpler). Update the other to import.
**Fix:** In `webui/inventory.py:181`, replace local function with `from toolforge_local_scan import _parse_frontmatter`. Test both call sites still work (multi-line values, missing closing `---`).

### F21 — Pick canonical `_normalize_name` (§3-5)
**Files:** `bin/toolforge_usage_detector.py:63` (canonical), `bin/toolforge_local_scan.py:152`, `bin/toolforge_db.py:54`.
**Fix:** In `local_scan.py` and `db.py`, replace with:
```python
from toolforge_usage_detector import _normalize_name
```
(Both already share `sys.path` via `bin/` placement.)
If circular import emerges, factor `_normalize_name` into a new tiny `bin/toolforge_names.py` module.

### F22 — Extract `MAX_PORT_SCAN`/`DEFAULT_PORT` to one source (§3-6)
**Files:** `webui/server.py:50` (keep), `webui/launch.py:21` (import).
**Fix:** Move constants to `server.py`:
```python
DEFAULT_PORT = 7321
MAX_PORT_SCAN = 50
DEFAULT_PORT_RANGE = range(DEFAULT_PORT, DEFAULT_PORT + MAX_PORT_SCAN)
```
In `launch.py`:
```python
from webui.server import DEFAULT_PORT_RANGE
```

### F23 — Refactor `_session_count_had_error` module-global (§3-7)
**File:** `bin/toolforge_db.py:36`
**Fix:** Drop the module-level `_session_count_had_error = False` line. Change `_current_session_count()` signature to return `(count, had_error)` tuple. Update `status()` (line 469-470) to unpack:
```python
count, had_error = _current_session_count()
```
Remove the latch-reset code inside `status()`.

### F24 — Deterministic project ID hash (§3-8)
**File:** `webui/inventory.py:329`
**Fix:** Replace `abs(hash(proj_path)) % 10**8` with:
```python
import hashlib
proj_id = hashlib.sha1(proj_path.encode("utf-8")).hexdigest()[:12]
```
Verify no callers depend on numeric type.

---

## §D — MED fixes — error handling & races (Wave 4)

### F25 — Quarantine corrupt config (§4-1)
**File:** `bin/toolforge_local_scan.py:113`
**Fix:** In `_load_config()`, on `json.JSONDecodeError`, rename the config to `.corrupt.<ts>`:
```python
except json.JSONDecodeError as exc:
    quarantine = path.with_suffix(f".corrupt.{int(time.time())}")
    try:
        path.rename(quarantine)
    except OSError:
        pass
    print(f"toolforge_local_scan: corrupt config quarantined to {quarantine}: {exc}", file=sys.stderr)
    return {}
```
Match the existing pattern in `_load_cache`.

### F26 — Stderr-log unreadable subdir (§4-2)
**File:** `bin/toolforge_local_scan.py:326`
**Fix:** Change `except OSError: continue` to:
```python
except OSError as exc:
    print(f"toolforge_local_scan: unreadable subdir {sub!r}: {exc}", file=sys.stderr)
    continue
```

### F27 — Off-by-one on file-count cap (§4-3)
**File:** `bin/toolforge_local_scan.py:312-314`
**Fix:** Move the `count >= MAX_FILES_SCANNED` check BEFORE the increment, OR document the +1 in a comment. Recommended:
```python
if count >= MAX_FILES_SCANNED:
    print(f"... cap reached at {MAX_FILES_SCANNED} ...", file=sys.stderr)
    break
count += 1
```

### F28 — Per-trigger lock on `/api/export` (§4-5)
**File:** `webui/exporter.py:225`
**Fix:** Add a module-level lock map keyed by trigger slug:
```python
import threading
_EXPORT_LOCKS: dict[str, threading.Lock] = {}
_EXPORT_LOCKS_MUTEX = threading.Lock()

def _trigger_lock(trigger: str) -> threading.Lock:
    with _EXPORT_LOCKS_MUTEX:
        return _EXPORT_LOCKS.setdefault(trigger, threading.Lock())
```
Wrap `export_flow` body in `with _trigger_lock(trigger):`.

### F29 — Reject blank/symbol-only trigger (§4-6)
**File:** `webui/exporter.py:266` (and API handler in `server.py`)
**Fix:** After `slug = slugify(trigger)`, check:
```python
if slug == "untitled-flow" or not slug.replace("-", "").isalnum():
    raise ValueError("Trigger must contain at least one alphanumeric character.")
```
Surface as 422 in the API handler.

### F30 — Empty hash file → exit 3 (§4-11)
**File:** `bin/toolforge_verify_fallback.py:71`
**Fix:** Replace:
```python
expected = hash_path.read_text(encoding="utf-8").strip().split()[0].lower()
```
With:
```python
tokens = hash_path.read_text(encoding="utf-8").strip().split()
if not tokens:
    print(f"toolforge_verify_fallback: empty hash file {hash_path}", file=sys.stderr)
    return 3
expected = tokens[0].lower()
```

### F31 — Stderr-log mid-read swallows (§4-9 × 2)
**File:** `bin/toolforge_usage_detector.py:158, 220`
**Fix:** Replace each `except OSError: pass` with:
```python
except OSError as exc:
    print(f"toolforge_usage_detector: read aborted on {path}: {exc}", file=sys.stderr)
    continue  # or pass, matching original control flow
```
Increment a `skipped` counter; return `(written, skipped)` from `persist_to_db` (combine with F07).

### F32 — Brittle `claude plugin list` parser (§4-10)
**File:** `bin/toolforge_local_scan.py:393-394`
**Fix:** Run `claude plugin list --json` if supported; parse as JSON. On older CLI versions where `--json` is unavailable, fall back to current line-walk but match a stricter header regex (e.g. `^\s*Plugin\s+Version\s+Source\s*$`) instead of `startswith`. Document the fallback path.

### F33 — Frontend toast on JSON parse error (§4-7)
**File:** `webui/static/app.js:380`
**Fix:** Replace `const j = await r.json().catch(() => ({}));` with:
```js
let j = {};
try {
  j = await r.json();
} catch {
  const body = await r.text().catch(() => '');
  showToast(`Preview error: ${r.status} ${r.statusText} — ${body.slice(0, 200)}`, 'error');
  return;
}
```

### F34 — Toast on malformed drop payload (§4-8)
**File:** `webui/static/app.js:200`
**Fix:** Wrap `JSON.parse(raw)` in try/catch with a toast:
```js
let parsed;
try { parsed = JSON.parse(raw); } catch (e) {
  showToast('Drop payload invalid JSON: ' + e.message, 'warn');
  return;
}
```

### F35 — Session ID env var verification (§2-10)
**Files:** `hooks/post-tool-use-counter.py:27-33`, `hooks/session-end-likert.py` (mirror)
**Fix:** Read Claude Code's actual env var name from official docs. Update both fallback lookups to the verified name. If the name is `CLAUDE_SESSION_ID` (likely), document it via a `# Verified against Claude Code docs 2026-05-27` comment.

### F36 — `--days N` parse failure (§4-12)
**File:** `bin/toolforge_dumb_scanner.py:108`
**Fix:** Replace `except ValueError: pass` with:
```python
except ValueError:
    print(f"toolforge_dumb_scanner: --days must be int, got {raw!r}", file=sys.stderr)
    return 2
```
(Or delete the whole file via F40 if §7-1 wins.)

---

## §E — LOW fixes — doc/comment rot (Wave 5)

### F37 — Reconcile timeout constants vs ARCHITECTURE.md (§6-1)
**Files:** `bin/toolforge_local_scan.py:57-58`, `ARCHITECTURE.md §4.5 + §5.4`.
**Decision required:** Either bump ARCH to match code (10/4) or shrink code to match ARCH (5/2). Recommended: shrink code to match ARCH (tighter security budget). Rename `SUBPROCESS_TIMEOUT_SECONDS` to `GIT_LOG_TIMEOUT_SECONDS = 2`. Set `CLAUDE_LIST_TIMEOUT_SECONDS = 5`. Update both subprocess call sites to use the renamed constant where appropriate.

### F38 — Stale `PATTERNS.md` reference (§5-1)
**File:** `bin/toolforge_install.py:529`
**Fix:** Replace `PATTERNS.md §7` with `ARCHITECTURE.md §7`.

### F39 — Drop `M5:`/`M6:` sprint tags (§5-2)
**Files:** `bin/toolforge_db.py:468`, `bin/toolforge_local_scan.py:612`
**Fix:** Edit each comment, drop the `M5:` / `M6:` prefix. Keep the explanation.

### F40 — Stale env-var doc in `_self_test` (§5-3)
**File:** `bin/toolforge_db.py:531`
**Fix:** Delete the `Override via TOOLFORGE_DB env var` line from the `_self_test` docstring. The env var isn't honored.

### F41 — Stale v0.1 deferral comment (§5-4)
**File:** `bin/toolforge_install.py:149-150`
**Fix:** Decide: either implement `claude mcp add` option-passing, or rewrite the comment as `# Options before server name (-e, -H, -s, -t, ...) are not supported. See SKETCHY_CODE_AUDIT.md#s5-4.`. Recommend the rewrite — implementation is out of scope for an audit pass.

### F42 — Stale "earlier drafts" paragraph (§5-5)
**File:** `bin/toolforge_install.py:18-24`
**Fix:** Trim the module docstring. Replace the "Earlier drafts wired..." paragraph with a single sentence: `Security review is the curator's responsibility; this installer only re-validates the command string.`

### F43 — Lying `_dedupe` docstring (§5-6)
**File:** `webui/inventory.py:475`
**Fix:** Rewrite the docstring to match actual behavior: `Dedup by (type, name, path or source) tuple. Plugin/mcp entries fall back to source label when path is empty.`

### F44 — Missing `git log` in `toolforge_local_scan` docstring (§5-7)
**File:** `bin/toolforge_local_scan.py:28`
**Fix:** Update the subprocess line in the module docstring to: `Subprocess: claude plugin list, claude mcp list, git log -1 --format=%ct, all shell=False with explicit timeouts.`

### F45 — Lying `get_rating_stats_bulk` complexity (§5-8)
**File:** `bin/toolforge_db.py:206`
**Fix:** Rewrite the docstring sentence to: `Single round-trip; N is the SQL IN-list size, not the number of network hops. SQL is O(N).`

---

## §F — Delete dead code (Wave 5)

### F46 — Delete `toolforge_dumb_scanner.py` (§7-1)
**File:** `bin/toolforge_dumb_scanner.py`
**Fix:** `git rm` the file. Search `docs/v0.2/01_END_PRODUCT.md` and `docs/v0.2/02_EXECUTION_PLAN.md` for `dumb_scanner` references — strike or replace with `usage_detector`.

### F47 — Delete `DISCOVERY_REPOS` module-level alias (§7-2)
**File:** `webui/inventory.py:162`
**Fix:** Delete the line `DISCOVERY_REPOS = _resolved_discovery_repos()` and the misleading "lazy" comment above it. Confirm no external readers (audit confirmed zero).

---

## §G — Unbounded ratings SELECT in `status()`

### F48 — Cap ratings SELECT (§1-or-§4 from audit doc)
**File:** `bin/toolforge_db.py:478-483`
**Fix:** Add a WHERE clause filtering to ratings within 365 days:
```python
cur.execute("SELECT tool_name, rating, rated_at FROM ratings WHERE julianday('now') - julianday(rated_at) <= 365 ORDER BY rated_at DESC")
```
Document the rationale: status() is a human-readable summary, not an export.

---

## §H — Wrap-up

### F49 — Document fixes in CHANGELOG.md
**File:** `CHANGELOG.md`
**Fix:** Append a new section `## Unreleased — sketchy code audit pass` listing each F## by audit anchor.

### F50 — Re-run AST parse + verification
**Fix:** Run `python -c "import ast; ast.parse(open(p, encoding='utf-8').read())"` on every touched `.py` file. Run `node --check` on touched `.js` if available. Spot-check WARN markers still align with line numbers (line drift expected after edits — line numbers in WARN comments are not used; only anchors).

---

## Skill assignment policy (for dispatcher)

Each ticket gets one agent. Rotate `subagent_type` across all available types. Inside the prompt, name one `Skill` tool invocation for variety. Suggested mapping:

| Fix family | Subagent type | Skill (prompt-named) |
|------------|---------------|----------------------|
| Format injection (F01) | pr-review-toolkit:silent-failure-hunter | superpowers:verification-before-completion |
| Drop ext (F02) | pr-review-toolkit:code-reviewer | gstack-careful |
| Vendor CDN (F03) | general-purpose | claude-api |
| Frontend toast (F04, F05, F33, F34) | feature-dev:code-architect | superpowers:executing-plans |
| Subprocess timeout (F06) | pr-review-toolkit:silent-failure-hunter | superpowers:systematic-debugging |
| sqlite catch (F07, F31) | pr-review-toolkit:silent-failure-hunter | gstack-investigate |
| Surface error (F08, F09) | feature-dev:code-reviewer | superpowers:executing-plans |
| Atomic write (F10, F11) | code-simplifier:code-simplifier | superpowers:executing-plans |
| Retry budget (F12) | pr-review-toolkit:code-reviewer | gstack-investigate |
| TOCTOU (F13) | feature-dev:code-explorer | superpowers:systematic-debugging |
| Prune active path (F14, F15) | caveman:cavecrew-builder | code-simplifier |
| Stream cap (F16) | pr-review-toolkit:type-design-analyzer | claude-api |
| Constants extract (F17-F22) | code-simplifier:code-simplifier | superpowers:executing-plans |
| Refactor global (F23) | caveman:cavecrew-builder | code-simplifier |
| sha1 swap (F24) | caveman:cavecrew-builder | claude-api |
| Quarantine config (F25) | pr-review-toolkit:silent-failure-hunter | gstack-careful |
| Stderr log (F26, F31, F36) | pr-review-toolkit:silent-failure-hunter | gstack-investigate |
| Off-by-one (F27) | feature-dev:code-reviewer | superpowers:verification-before-completion |
| Lock per-trigger (F28) | feature-dev:code-architect | gstack-investigate |
| Reject input (F29, F30) | pr-review-toolkit:code-reviewer | superpowers:executing-plans |
| Brittle parser (F32) | feature-dev:code-explorer | superpowers:systematic-debugging |
| Env var verify (F35) | claude-code-guide | gstack-investigate |
| Timeout reconcile (F37) | pr-review-toolkit:comment-analyzer | superpowers:executing-plans |
| Doc rot (F38-F45) | pr-review-toolkit:comment-analyzer | (no extra skill) |
| Delete dead (F46, F47) | code-simplifier:code-simplifier | superpowers:executing-plans |
| status() cap (F48) | pr-review-toolkit:code-reviewer | gstack-investigate |
| Changelog (F49) | general-purpose | (no extra skill) |
| Verify pass (F50) | pr-review-toolkit:pr-test-analyzer | superpowers:verification-before-completion |

Rotation ensures no subagent_type runs more than ~5 times across all 50 tickets.
