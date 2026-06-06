# ToolForge Fix Conventions

Shared patterns for all fix-pass agents. Reference this doc when applying tickets from `FIX_PLAYBOOK.md`.

## 1. Atomic file write

Use this pattern everywhere `path.write_text(...)` lives on a hot path that could crash mid-write:

```python
def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(tmp, path)
```

For atomic-write of a directory of files:

```python
import shutil

tmp_dir = target_dir.with_suffix(target_dir.suffix + ".tmp")
try:
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    # ... write all files into tmp_dir ...
    if target_dir.exists():
        shutil.rmtree(target_dir)
    os.replace(tmp_dir, target_dir)
except Exception:
    shutil.rmtree(tmp_dir, ignore_errors=True)
    raise
```

## 2. Validator pattern (regex + raise)

```python
TOOL_NAME_RE = re.compile(r"^[a-z0-9._@/\-]{1,80}$")

def _validate_tool_name(name: str) -> None:
    if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
        raise ValueError(f"invalid tool_name: {name!r}")
```

Always raise at the boundary, never silently coerce.

## 3. Subprocess with timeout + group kill

```python
try:
    result = subprocess.run(
        argv,
        timeout=TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
except subprocess.TimeoutExpired as exc:
    # On POSIX, kill the process group; on Windows the timeout itself terminates.
    print(f"... subprocess timed out after {TIMEOUT_SECONDS}s", file=sys.stderr)
    return EXIT_TIMEOUT
```

## 4. Error surfacing — never return empty on transient failure

When a function reads from disk/SQL/network and the caller cannot distinguish "no data" from "error":

```python
# BAD
try:
    return load(...)
except Exception:
    return []

# GOOD
def load_with_status(...) -> tuple[list, str | None]:
    try:
        return load(...), None
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        return [], f"load failed: {exc}"
```

Caller decides whether to suppress or surface.

## 5. Catch all expected exception classes

Database persistence loops must catch `sqlite3.Error` in addition to `OSError`/`ValueError`. JSON I/O loops must catch `json.JSONDecodeError`. Subprocess loops must catch `subprocess.SubprocessError`.

Never use bare `except:` or `except Exception:` on a hot path.

## 6. Module-import boundaries

`bin/` modules are addressable from `webui/` via `sys.path.insert(0, str(BIN_DIR))`. Use this for shared constants/helpers. Do NOT copy-paste.

When extracting a shared symbol, prefer:
- Define once in the file where it's most-densely used.
- Import in others.

Only factor into a new module when the symbol is genuinely shared across 3+ files AND none of them feels like the natural home.

## 7. Comment hygiene

- Never write WHAT-comments (the code already says what).
- Always write WHY-comments where: (a) a workaround for a specific platform bug, (b) a non-obvious invariant, (c) a security-significant defense the reader might "clean up".
- Drop sprint tags (`M5:`, `M6:`, `H5:`, `PR-1234:`). Future maintainers cannot decode them.
- Drop references to deleted features ("Earlier drafts...").
- WARN markers stay — they're a navigation aid, not a sprint tag.

## 8. Static frontend

- No external CDN dependencies. Vendor into `webui/static/vendor/`.
- No external font imports. System-font stack only.
- Error handling: every `fetch()` must check `r.ok` and surface to a toast.
- Every `JSON.parse()` of user-controlled or external input is in a try/catch with a user-visible failure mode.

## 9. Toast helper (frontend)

If a toast helper doesn't exist in `app.js`, add this minimal one:

```js
function showToast(msg, level = 'info') {
  const el = document.createElement('div');
  el.className = `toast toast-${level}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}
```

CSS hook in `styles.css`:
```css
.toast { position: fixed; bottom: 16px; right: 16px; padding: 8px 12px; border-radius: 6px; max-width: 480px; font-size: 13px; z-index: 9999; }
.toast-info { background: #1f2937; color: #fff; }
.toast-warn { background: #b45309; color: #fff; }
.toast-error { background: #b91c1c; color: #fff; }
```

## 10. Verification after each fix

- Python: `python -c "import ast; ast.parse(open('FILE', encoding='utf-8').read())"`
- JS: `node --check FILE.js` (if Node available)
- Re-run `_self_test` inside the file if present
- Re-Read the touched section before declaring done

## 11. WARN marker handling

WARN markers added in the previous audit pass remain in place AFTER the fix lands. Update each WARN's prose to say "FIXED in F##" rather than deleting it. The marker still serves as a breadcrumb for future code archaeologists. Example:

```python
# WARN: see SKETCHY_CODE_AUDIT.md#s3-1 — FIXED in F17 (constant extracted to toolforge_db.DECAY_HALFLIFE_DAYS)
```

Or delete the marker entirely if the underlying smell is fully gone. Use judgment.

## 12. Do not refactor beyond ticket scope

A subprocess-timeout ticket does NOT include "while I'm here, let me also add retries". Fix only what the ticket asks. Out-of-scope changes break review and inflate diff.

If you find a new sketchy spot during your ticket, add a `# WARN: NEW — <description>` comment but do not fix it. Surface in the agent's report.
