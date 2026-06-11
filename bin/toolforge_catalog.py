#!/usr/bin/env python3
"""ToolForge curated catalog — cold-start seeding and catalog-aware routing.

The catalog is a hand-reviewed list of high-quality MCP servers, skills, and
plugins. It serves two purposes:

1. Cold-start seeding: On first install, synthetic ratings are inserted into
   the DB so the Bayesian ranker has signal from day one rather than returning
   generic equal-score results.

2. Catalog-aware routing: When the TF-IDF router finds no installed skill
   above threshold, this module performs keyword matching against catalog
   entries and returns suggestions with installed=False so the hook can
   suggest /toolforge-hunt or /toolforge <category>.

CLI:
  toolforge_catalog.py list [--category <cat>]      list catalog entries
  toolforge_catalog.py seed                          seed DB (idempotent)
  toolforge_catalog.py suggest <prompt text...>      keyword-match catalog
  toolforge_catalog.py --self-test
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent          # bin/
CATALOG_DIR = HERE.parent / "catalog"           # catalog/ at repo root
CATALOG_FILE = CATALOG_DIR / "catalog.json"
MANIFEST_FILE = CATALOG_DIR / "manifest.json"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# ── stop words ────────────────────────────────────────────────────────────────
STOP_WORDS: set[str] = {
    "a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "at",
    "is", "it", "be", "as", "by", "with", "this", "that", "from", "are",
    "was", "has", "have", "had", "do", "use", "make", "get", "set", "run",
    "add", "new", "all", "my", "me", "we", "i", "you", "can", "will", "so",
    "how", "what", "which", "when", "where", "who", "why", "then", "now",
    "need", "want", "help", "write", "create", "build", "fix", "please",
}

_catalog_cache: Optional[list[dict]] = None
# Derived caches for suggest()'s hot path. Built once from _catalog_cache and
# invalidated together with it. Never populated from a failed/empty load (see
# load_catalog) — guarded on _catalog_cache being set.
_index_cache: Optional[dict[str, list[str]]] = None
_entry_map_cache: Optional[dict[str, dict]] = None


# ── loaders ───────────────────────────────────────────────────────────────────

def load_catalog(category: Optional[str] = None) -> list[dict]:
    """Load all catalog entries, optionally filtered to a category slug.

    Entries lacking a non-empty "name" are dropped (consumers index by name
    and would otherwise KeyError). A malformed catalog file degrades to an
    empty list rather than killing routing fallback; the failure is not cached
    so a fixed/restored file is picked up on the next call.
    """
    global _catalog_cache
    if _catalog_cache is None:
        if not CATALOG_FILE.exists():
            return []
        try:
            with open(CATALOG_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"toolforge_catalog: catalog unreadable, ignoring ({exc})", file=sys.stderr)
            return []  # do not cache the failure — retry on next call
        if not isinstance(raw, list):
            print("toolforge_catalog: catalog is not a JSON list, ignoring", file=sys.stderr)
            return []
        kept = [e for e in raw if isinstance(e, dict) and e.get("name")]
        skipped = len(raw) - len(kept)
        if skipped:
            print(f"toolforge_catalog: skipped {skipped} catalog entr(ies) missing a name", file=sys.stderr)
        _catalog_cache = kept
    if category is None:
        return list(_catalog_cache)
    cat_lower = category.lower()
    return [e for e in _catalog_cache if cat_lower in e.get("categories", [])]


def load_manifest() -> dict:
    if not MANIFEST_FILE.exists():
        return {}
    with open(MANIFEST_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_entry(name: str) -> Optional[dict]:
    return next((e for e in load_catalog() if e["name"] == name), None)


# ── keyword matching ──────────────────────────────────────────────────────────

def _build_keyword_index(entries: list[dict]) -> dict[str, list[str]]:
    """Map each meaningful word → list of entry names containing it."""
    idx: dict[str, list[str]] = {}
    for entry in entries:
        words: set[str] = set()
        for tag in entry.get("tags", []):
            for w in tag.lower().split():
                words.add(w)
        for w in entry.get("description", "").lower().split():
            words.add(w.strip(".,;:!?()"))
        for cat in entry.get("categories", []):
            words.add(cat.lower())
        words.add(entry["name"].lower())
        for word in words:
            if len(word) > 2 and word not in STOP_WORDS:
                idx.setdefault(word, []).append(entry["name"])
    return idx


def suggest(prompt: str, top_k: int = 3, min_score: int = 1) -> list[dict]:
    """Keyword-match a user prompt against catalog entries.

    Returns up to top_k entries with match_score, installed=False,
    catalog_only=True. Returns [] when nothing matches.
    """
    global _index_cache, _entry_map_cache
    entries = load_catalog()
    if not entries:
        return []

    # Catalog is static per process; build the index + entry_map once. Only
    # cache when load_catalog actually cached (i.e. _catalog_cache is set) so a
    # failed/empty load is retried rather than frozen, matching load_catalog.
    if _catalog_cache is not None and _index_cache is not None and _entry_map_cache is not None:
        idx = _index_cache
        entry_map = _entry_map_cache
    else:
        idx = _build_keyword_index(entries)
        entry_map = {e["name"]: e for e in entries}
        if _catalog_cache is not None:
            _index_cache = idx
            _entry_map_cache = entry_map

    prompt_words = {
        w.lower().strip(".,;:!?()") for w in prompt.split()
        if len(w) > 2 and w.lower() not in STOP_WORDS
    }

    scores: dict[str, int] = {}
    for word in sorted(prompt_words):
        for name in idx.get(word, []):
            scores[name] = scores.get(name, 0) + 1

    # Sort by descending score, then name ascending — deterministic ties.
    ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:top_k]
    return [
        {
            **entry_map[name],
            "match_score": score,
            "installed": False,
            "catalog_only": True,
        }
        for name, score in ranked
        if score >= min_score
    ]


# ── DB seeding ────────────────────────────────────────────────────────────────

def is_seeded() -> bool:
    """Return True if the catalog has already been seeded into the DB.

    Intentionally does NOT call init_db() to avoid circular calls when
    init_db() itself triggers seeding on brand-new installs.
    """
    try:
        import toolforge_db  # type: ignore
        if not toolforge_db.DB_PATH.exists():
            return False
        conn = toolforge_db._connect()
        row = conn.execute(
            "SELECT COUNT(*) FROM installs WHERE tool_name = '__catalog_seeded__'"
        ).fetchone()
        conn.close()
        return bool(row and row[0] > 0)
    except Exception:
        return False


def seed_db() -> int:
    """Insert synthetic ratings for catalog entries. Idempotent.

    Returns the number of rating rows inserted (0 if already seeded).

    Synthetic ratings use a fixed past date so they decay at the standard
    75-day half-life. Real user ratings overtake them naturally.

    Safe to call from init_db() — does NOT call init_db() internally.
    """
    if is_seeded():
        return 0

    try:
        import toolforge_db  # type: ignore
        conn = toolforge_db._connect()
    except Exception as exc:
        print(f"seed_db: DB unavailable ({exc})", file=sys.stderr)
        return 0

    # Guard: re-check inside the connection in case of concurrent seeding
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM installs WHERE tool_name = '__catalog_seeded__'"
        ).fetchone()
        if row and row[0] > 0:
            conn.close()
            return 0
    except Exception:
        conn.close()
        return 0

    entries = load_catalog()
    count = 0
    seed_date = "2026-01-01T00:00:00.000Z"

    for entry in entries:
        raw = float(entry.get("cold_start_rating", 3.8))
        weight = int(entry.get("cold_start_weight", 5))
        name = entry["name"]

        low = max(1, min(4, int(raw)))
        high = min(5, low + 1)
        high_count = round(weight * (raw - low))
        low_count = weight - high_count

        for _ in range(low_count):
            conn.execute(
                "INSERT INTO ratings (tool_name, rating, rated_at) VALUES (?, ?, ?)",
                (name, low, seed_date),
            )
            count += 1
        for _ in range(high_count):
            conn.execute(
                "INSERT INTO ratings (tool_name, rating, rated_at) VALUES (?, ?, ?)",
                (name, high, seed_date),
            )
            count += 1

    # Marker so we never seed twice
    conn.execute(
        "INSERT INTO installs (tool_name, category, approved, installed_at) "
        "VALUES (?, ?, ?, ?)",
        ("__catalog_seeded__", "__system__", 0, seed_date),
    )
    conn.commit()
    conn.close()
    return count


# ── display ───────────────────────────────────────────────────────────────────

def _render_entry(entry: dict) -> str:
    api_note = ""
    if entry.get("requires_api_key"):
        env = entry.get("api_key_env", "API_KEY")
        api_note = f"  [needs {env}]"
    official = f" ({entry['official_org']})" if entry.get("official") else ""
    cats = ", ".join(entry.get("categories", []))
    return (
        f"  {entry['name']:<28} {entry.get('display_name','')}{official}\n"
        f"    {entry.get('description','')[:80]}\n"
        f"    Categories: {cats}{api_note}\n"
        f"    Install: {entry.get('install_command','')}\n"
    )


# ── self-test ─────────────────────────────────────────────────────────────────

def _self_test() -> int:
    passed = failed = 0

    all_entries = load_catalog()
    if all_entries:
        print(f"OK: load_catalog returned {len(all_entries)} entries")
        passed += 1
    else:
        print("FAIL: load_catalog returned empty list")
        failed += 1

    coding = load_catalog("coding")
    ui = load_catalog("ui")
    if coding and ui and len(coding) < len(all_entries):
        print(f"OK: category filter — coding={len(coding)}, ui={len(ui)}, all={len(all_entries)}")
        passed += 1
    else:
        print(f"FAIL: category filter — coding={len(coding)} ui={len(ui)} all={len(all_entries)}")
        failed += 1

    results = suggest("browser automation e2e testing playwright")
    if results and results[0].get("catalog_only") is True:
        print(f"OK: suggest('browser automation e2e testing') -> {results[0]['name']} (score={results[0]['match_score']})")
        passed += 1
    else:
        print(f"FAIL: suggest returned: {results}")
        failed += 1

    empty = suggest("xyzzy frobnicate quux")
    if empty == []:
        print("OK: suggest returns [] for gibberish")
        passed += 1
    else:
        print(f"FAIL: suggest returned non-empty for gibberish: {empty}")
        failed += 1

    required = {"name", "description", "install_command", "source_url", "type", "categories"}
    missing = [e["name"] for e in all_entries if not required.issubset(e.keys())]
    if not missing:
        print("OK: all entries have required fields")
        passed += 1
    else:
        print(f"FAIL: entries missing required fields: {missing}")
        failed += 1

    allowed_bins = {"claude", "npx", "uvx", "npm", "pip", "pipx", "uv"}
    bad = [e["name"] for e in all_entries
           if not e.get("install_command", "").split()[0] in allowed_bins]
    if not bad:
        print("OK: all install commands start with an allowed binary")
        passed += 1
    else:
        print(f"FAIL: disallowed install binary in: {bad}")
        failed += 1

    import tempfile
    try:
        import toolforge_db  # type: ignore
        original_db = toolforge_db.DB_PATH
        tmp = Path(tempfile.mkdtemp()) / "catalog_seed_test.db"
        toolforge_db.DB_PATH = tmp
        global _catalog_cache, _index_cache, _entry_map_cache
        _catalog_cache = _index_cache = _entry_map_cache = None

        # init_db() on a brand-new DB triggers seeding as a side effect.
        toolforge_db.init_db()

        conn = toolforge_db._connect()
        row_count = conn.execute(
            "SELECT COUNT(*) FROM ratings WHERE tool_name != '__catalog_seeded__'"
        ).fetchone()[0]
        conn.close()

        if row_count > 0 and is_seeded():
            print(f"OK: init_db seeds catalog on first install ({row_count} rating rows, is_seeded=True)")
            passed += 1
        else:
            print(f"FAIL: row_count={row_count}, is_seeded={is_seeded()}")
            failed += 1

        # Idempotency: explicit seed_db() call returns 0 when already seeded.
        n2 = seed_db()
        if n2 == 0:
            print("OK: seed_db idempotent — returns 0 when already seeded")
            passed += 1
        else:
            print(f"FAIL: seed_db returned {n2} on second call (expected 0)")
            failed += 1

        _catalog_cache = _index_cache = _entry_map_cache = None
    except Exception as exc:
        print(f"FAIL: seed_db test raised {exc}")
        failed += 1
    finally:
        toolforge_db.DB_PATH = original_db
        _catalog_cache = _index_cache = _entry_map_cache = None
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "Usage:\n"
            "  toolforge_catalog.py list [--category <cat>]\n"
            "  toolforge_catalog.py seed\n"
            "  toolforge_catalog.py suggest <prompt text...>\n"
            "  toolforge_catalog.py --self-test\n"
            "\nCategories: coding, writing, data, automation, ui\n"
        )
        return 0

    cmd = argv[0]

    if cmd == "--self-test":
        return _self_test()

    if cmd == "list":
        category = None
        if "--category" in argv:
            idx = argv.index("--category")
            if idx + 1 < len(argv):
                category = argv[idx + 1]
        entries = load_catalog(category)
        manifest = load_manifest()
        label = f"category={category}" if category else "all"
        print(f"ToolForge Catalog v{manifest.get('version','?')} — {len(entries)} entries ({label})\n")
        for e in entries:
            print(_render_entry(e))
        api_count = sum(1 for e in entries if e.get("requires_api_key"))
        if api_count:
            print(f"Note: {api_count} tool(s) require API keys.")
        return 0

    if cmd == "seed":
        count = seed_db()
        if count == 0:
            print("Already seeded — nothing to do. (Remove ~/.claude/toolforge.db to re-seed.)")
        else:
            entries = load_catalog()
            print(f"Seeded {count} synthetic rating rows for {len(entries)} catalog entries.")
        return 0

    if cmd == "suggest":
        prompt = " ".join(argv[1:]).strip()
        if not prompt:
            print("suggest requires a prompt", file=sys.stderr)
            return 2
        results = suggest(prompt)
        if not results:
            print("No catalog match found.")
            return 0
        print(json.dumps(results, indent=2))
        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
