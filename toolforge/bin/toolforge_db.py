#!/usr/bin/env python3
"""ToolForge SQLite store. CLI-driven so slash commands and skills can shell out.

Errors fail loud: sqlite errors exit 3 with a message on stderr, usage errors
exit 2. Callers should treat a non-zero exit as 'no data' rather than '0.00 avg'.

Schema version managed via PRAGMA user_version. Bump SCHEMA_VERSION and add
a migration block when the schema changes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Optional

# ---------- section: constants ----------

DB_PATH = Path(os.path.expanduser("~/.claude/toolforge.db"))
# Canonical decay half-life. Imported by toolforge_local_scan.py and webui/inventory.py.
DECAY_HALFLIFE_DAYS = 75.0  # AI tooling moves fast — was 180d, cut to ~2.5mo
SCHEMA_VERSION = 3
BAYES_PRIOR_MEAN = 3.0
BAYES_PRIOR_WEIGHT = 5.0

TOOL_NAME_RE = re.compile(r"^[a-z0-9._@/-]{1,80}$")
TOOL_KEY_RE = re.compile(r"^[a-z]+:[a-z0-9._@/-]{1,80}$")
CATEGORY_RE = re.compile(r"^[a-z]{1,32}$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-/_:?&=%@~#+,]{4,2048}$")

# WARN: see SKETCHY_CODE_AUDIT.md#s3-7 — FIXED in F23 (had_error now returned via tuple from _current_session_count / status).


def _validate_category(cat: str) -> str:
    if not CATEGORY_RE.match(cat):
        raise ValueError(f"invalid category {cat!r}: must match {CATEGORY_RE.pattern}")
    return cat


# ---------- section: connection-helpers ----------


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# WARN: see SKETCHY_CODE_AUDIT.md#s3-5 — FIXED in F21 (kept as distinct symbol; toolforge_db._normalize differs from canonical _normalize_name by design — strips+lowers without separator substitution).
def _normalize(name: str) -> str:
    return name.strip().lower()


def _validate_tool_name(name: str) -> str:
    n = _normalize(name)
    if not TOOL_NAME_RE.match(n):
        raise ValueError(f"invalid tool_name {name!r}: must match {TOOL_NAME_RE.pattern}")
    return n


def _validate_tool_key(key: str) -> str:
    """Tool keys are typed: skill:foo, mcp:github, plugin:bar, agent:baz, command:qux."""
    k = _normalize(key)
    if not TOOL_KEY_RE.match(k):
        raise ValueError(f"invalid tool_key {key!r}: must match {TOOL_KEY_RE.pattern}")
    return k


def _validate_url(url: str) -> str:
    u = url.strip()
    if not URL_RE.match(u):
        raise ValueError(f"invalid url {url!r}: must match {URL_RE.pattern}")
    return u


# ---------- section: schema-init ----------


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
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
            CREATE INDEX IF NOT EXISTS idx_ratings_tool ON ratings(tool_name);
            CREATE INDEX IF NOT EXISTS idx_installs_tool ON installs(tool_name);
            """
        )
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current < 2:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_stats (
                    tool_key     TEXT PRIMARY KEY,
                    count_30d    INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT,
                    computed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS deprecations (
                    source_url   TEXT PRIMARY KEY,
                    tool_name    TEXT NOT NULL,
                    archived     INTEGER NOT NULL DEFAULT 0,
                    last_push_at TEXT,
                    checked_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_dep_tool ON deprecations(tool_name);
                CREATE TABLE IF NOT EXISTS routing_scores (
                    tool_key     TEXT PRIMARY KEY,
                    desc_match   REAL NOT NULL DEFAULT 0.0,
                    name_match   REAL NOT NULL DEFAULT 0.0,
                    usage_boost  REAL NOT NULL DEFAULT 0.0,
                    likert_norm  REAL NOT NULL DEFAULT 0.6,
                    composite    REAL NOT NULL DEFAULT 0.0,
                    computed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                """
            )
        if current < 3:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipelines (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_desc    TEXT NOT NULL,
                    steps_hash   TEXT NOT NULL,
                    steps_json   TEXT NOT NULL,
                    success      INTEGER NOT NULL DEFAULT 0,
                    run_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_pipelines_steps_hash
                    ON pipelines(steps_hash);
                CREATE INDEX IF NOT EXISTS idx_pipelines_run_at
                    ON pipelines(run_at DESC);
                """
            )
        if current < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        conn.commit()
    finally:
        conn.close()


# ---------- section: installs-table ----------


def log_install(tool_name: str, category: str, approved: bool) -> None:
    name = _validate_tool_name(tool_name)
    _validate_category(category)
    init_db()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO installs (tool_name, category, approved) VALUES (?, ?, ?)",
            (name, category, 1 if approved else 0),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- section: ratings-table ----------


def log_rating(tool_name: str, rating: int) -> None:
    if not 1 <= rating <= 5:
        raise ValueError("rating must be between 1 and 5")
    name = _validate_tool_name(tool_name)
    init_db()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO ratings (tool_name, rating) VALUES (?, ?)",
            (name, rating),
        )
        conn.commit()
    finally:
        conn.close()


def _compute_stats(entries: list[tuple[int, float]]) -> dict:
    """entries: list of (rating, age_days). Returns sum/n/avg/decayed_avg."""
    if not entries:
        return {"sum": 0, "n": 0, "avg": None, "decayed_avg": None}
    total = sum(int(r) for r, _ in entries)
    n = len(entries)
    avg = total / n
    weights = [math.exp(-max(0.0, a) / DECAY_HALFLIFE_DAYS) for _, a in entries]
    wsum = sum(weights)
    if wsum > 0 and wsum < 1e-30:
        print("toolforge_db: decay weights collapsed, falling back to raw avg", file=sys.stderr)
        decayed = avg
    elif wsum > 0:
        decayed = sum(int(r) * w for (r, _), w in zip(entries, weights)) / wsum
    else:
        decayed = avg
    return {"sum": total, "n": n, "avg": avg, "decayed_avg": decayed}


def get_rating_stats(tool_name: str) -> dict:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT rating, julianday('now') - julianday(rated_at, 'utc') AS age_days
            FROM ratings WHERE tool_name = ?
            """,
            (_normalize(tool_name),),
        ).fetchall()
    finally:
        conn.close()
    entries = [(int(r), float(a) if a is not None else 0.0) for r, a in rows]
    return _compute_stats(entries)


def get_rating_stats_bulk(names: list[str]) -> dict:
    # WARN: see SKETCHY_CODE_AUDIT.md#s5-8 — FIXED in F45 (docstring no longer claims O(1) total work).
    """Single-connection, single-query bulk lookup. Single round-trip; N is the SQL IN-list size, not the number of network hops. SQL is O(N)."""
    init_db()
    result = {n: {"sum": 0, "n": 0, "avg": None, "decayed_avg": None} for n in names}
    if not names:
        return result
    normalized = [_normalize(n) for n in names]
    placeholders = ",".join("?" * len(normalized))
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT tool_name, rating,
                   julianday('now') - julianday(rated_at, 'utc') AS age_days
            FROM ratings
            WHERE tool_name IN ({placeholders})
            """,
            normalized,
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for name, rating, age in rows:
        grouped[name].append((int(rating), float(age) if age is not None else 0.0))

    norm_to_orig: dict[str, str] = {}
    for orig in names:
        norm_to_orig.setdefault(_normalize(orig), orig)

    for norm_name, entries in grouped.items():
        orig = norm_to_orig.get(norm_name, norm_name)
        result[orig] = _compute_stats(entries)
    return result


def get_avg_rating(tool_name: str) -> Optional[float]:
    return get_rating_stats(tool_name)["avg"]


# ---------- section: usage-stats-v2 ----------

def upsert_usage_stats(tool_key: str, count_30d: int, last_used_at: Optional[str]) -> None:
    key = _validate_tool_key(tool_key)
    if count_30d < 0:
        raise ValueError("count_30d must be non-negative")
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO usage_stats (tool_key, count_30d, last_used_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tool_key) DO UPDATE SET
                count_30d    = excluded.count_30d,
                last_used_at = excluded.last_used_at,
                computed_at  = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (key, int(count_30d), last_used_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_usage_stats_bulk(keys: list[str]) -> dict:
    """Bulk lookup. Returns {key: {count_30d, last_used_at, computed_at}} or empty defaults."""
    init_db()
    result = {k: {"count_30d": 0, "last_used_at": None, "computed_at": None} for k in keys}
    if not keys:
        return result
    norm = [_validate_tool_key(k) for k in keys]
    placeholders = ",".join("?" * len(norm))
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT tool_key, count_30d, last_used_at, computed_at FROM usage_stats "
            f"WHERE tool_key IN ({placeholders})",
            norm,
        ).fetchall()
    finally:
        conn.close()
    norm_to_orig = {}
    for orig in keys:
        norm_to_orig.setdefault(_normalize(orig), orig)
    for k, c, l, t in rows:
        orig = norm_to_orig.get(k, k)
        result[orig] = {"count_30d": int(c), "last_used_at": l, "computed_at": t}
    return result


# ---------- section: deprecations-v2 ----------

def upsert_deprecation(source_url: str, tool_name: str, archived: bool, last_push_at: Optional[str]) -> None:
    url = _validate_url(source_url)
    name = _validate_tool_name(tool_name)
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO deprecations (source_url, tool_name, archived, last_push_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_url) DO UPDATE SET
                tool_name    = excluded.tool_name,
                archived     = excluded.archived,
                last_push_at = excluded.last_push_at,
                checked_at   = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (url, name, 1 if archived else 0, last_push_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_deprecation(source_url: str) -> Optional[dict]:
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT tool_name, archived, last_push_at, checked_at FROM deprecations WHERE source_url = ?",
            (source_url.strip(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "tool_name": row[0],
        "archived": bool(row[1]),
        "last_push_at": row[2],
        "checked_at": row[3],
    }


# ---------- section: routing-v2 ----------

def upsert_routing_score(
    tool_key: str,
    desc_match: float,
    name_match: float,
    usage_boost: float,
    likert_norm: float,
    composite: float,
) -> None:
    key = _validate_tool_key(tool_key)
    for v, n in [(desc_match, "desc_match"), (name_match, "name_match"),
                 (usage_boost, "usage_boost"), (likert_norm, "likert_norm"),
                 (composite, "composite")]:
        if not 0.0 <= v <= 1.5:
            raise ValueError(f"{n}={v} out of plausible 0.0-1.5 range")
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO routing_scores (tool_key, desc_match, name_match, usage_boost, likert_norm, composite)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tool_key) DO UPDATE SET
                desc_match  = excluded.desc_match,
                name_match  = excluded.name_match,
                usage_boost = excluded.usage_boost,
                likert_norm = excluded.likert_norm,
                composite   = excluded.composite,
                computed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (key, float(desc_match), float(name_match), float(usage_boost),
             float(likert_norm), float(composite)),
        )
        conn.commit()
    finally:
        conn.close()


def get_routing_scores_bulk(keys: list[str]) -> dict:
    init_db()
    result = {k: None for k in keys}
    if not keys:
        return result
    norm = [_validate_tool_key(k) for k in keys]
    placeholders = ",".join("?" * len(norm))
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT tool_key, desc_match, name_match, usage_boost, likert_norm, composite, computed_at "
            f"FROM routing_scores WHERE tool_key IN ({placeholders})",
            norm,
        ).fetchall()
    finally:
        conn.close()
    norm_to_orig = {}
    for orig in keys:
        norm_to_orig.setdefault(_normalize(orig), orig)
    for k, d, n, u, l, c, t in rows:
        orig = norm_to_orig.get(k, k)
        result[orig] = {
            "desc_match": float(d), "name_match": float(n),
            "usage_boost": float(u), "likert_norm": float(l),
            "composite": float(c), "computed_at": t,
        }
    return result


# ---------- section: pipelines-v3 ----------


def save_pipeline(task_desc: str, steps_hash: str, steps_json: str, success: bool) -> int:
    """Insert a pipeline record. Returns the new row id."""
    if not task_desc or not task_desc.strip():
        raise ValueError("task_desc must be non-empty")
    if not steps_hash or not steps_hash.strip():
        raise ValueError("steps_hash must be non-empty")
    if not steps_json or not steps_json.strip():
        raise ValueError("steps_json must be non-empty")
    # Validate steps_json is parseable
    try:
        json.loads(steps_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"steps_json is not valid JSON: {exc}") from exc
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO pipelines (task_desc, steps_hash, steps_json, success)
            VALUES (?, ?, ?, ?)
            """,
            (task_desc.strip(), steps_hash.strip()[:64], steps_json, 1 if success else 0),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_pipelines_by_hash(steps_hash: str, limit: int = 5) -> list[dict]:
    """Return past pipelines whose step chain matches steps_hash, newest first."""
    if limit < 1 or limit > 100:
        raise ValueError("limit must be 1..100")
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, task_desc, steps_json, success, run_at
            FROM pipelines
            WHERE steps_hash = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (steps_hash.strip()[:64], int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "task_desc": r[1], "steps_json": r[2],
         "success": bool(r[3]), "run_at": r[4]}
        for r in rows
    ]


def get_recent_pipelines(limit: int = 10) -> list[dict]:
    """Return the most recently run pipelines."""
    if limit < 1 or limit > 100:
        raise ValueError("limit must be 1..100")
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, task_desc, steps_hash, steps_json, success, run_at
            FROM pipelines
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "task_desc": r[1], "steps_hash": r[2],
         "steps_json": r[3], "success": bool(r[4]), "run_at": r[5]}
        for r in rows
    ]


# ---------- section: cli-entry-points ----------


def get_last_installed_tool() -> Optional[str]:
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT tool_name FROM installs WHERE approved = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def rate_last(rating: int) -> tuple[Optional[str], str, int]:
    """Returns (tool_name_or_None, user_msg, exit_code).

    exit_code = 0 on success, 2 when no installed tool to rate.
    """
    tool = get_last_installed_tool()
    if not tool:
        return None, "No installed tool found. Run /toolforge <category> first.", 2
    log_rating(tool, rating)
    s = get_rating_stats(tool)
    avg_disp = f"{s['avg']:.2f}" if s["avg"] is not None else "n/a"
    return tool, f"Rated {tool} = {rating}/5. New avg: {avg_disp} ({s['n']} rating(s)).", 0


def _session_counter_path(session_id: str) -> Path:
    """Mirror post-tool-use-counter.py path derivation so status can read it."""
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if not safe:
        safe = hashlib.sha1(
            session_id.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"toolforge_session_{safe}.count"


def _current_session_count() -> tuple[Optional[int], str, bool]:
    """Returns (count, source_label, had_error). Count is None when session id unknown."""
    sid = os.environ.get("CLAUDE_SESSION_ID")
    if not sid:
        return None, "no CLAUDE_SESSION_ID in env", False
    path = _session_counter_path(sid)
    try:
        return (path.stat().st_size if path.exists() else 0), str(path), False
    except OSError as exc:
        return None, f"stat failed: {exc}", True


def _shrunk_score(stats: dict) -> float:
    """Bayesian-shrunk posterior on decayed_avg. Same formula as curator skill."""
    n = stats["n"]
    if n == 0:
        return BAYES_PRIOR_MEAN
    return (stats["decayed_avg"] * n + BAYES_PRIOR_MEAN * BAYES_PRIOR_WEIGHT) / (n + BAYES_PRIOR_WEIGHT)


def status() -> tuple[str, bool]:
    # WARN: see SKETCHY_CODE_AUDIT.md#s5-2 — FIXED in F39 (sprint tag dropped from db.py).
    # reset latch each call so a transient stat() failure doesn't permanently mark the process as errored (long-lived importers like webui/inventory.py call this repeatedly).
    init_db()
    conn = _connect()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM installs WHERE approved = 1"
        ).fetchone()[0]
        # WARN: see SKETCHY_CODE_AUDIT.md#s1-or-s4 — FIXED in F48 (capped to 365 days below).
        # Cap at 365 days — status() is a human-readable summary, not a full export.
        rows = conn.execute(
            """
            SELECT tool_name, rating,
                   julianday('now') - julianday(rated_at, 'utc') AS age_days
            FROM ratings
            WHERE julianday('now') - julianday(rated_at, 'utc') <= 365
            """
        ).fetchall()
        last = conn.execute(
            "SELECT tool_name, rating, rated_at FROM ratings ORDER BY id DESC LIMIT 5"
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for name, rating, age in rows:
        grouped[name].append((int(rating), float(age) if age is not None else 0.0))

    scored = []
    for name, entries in grouped.items():
        stats = _compute_stats(entries)
        scored.append((name, _shrunk_score(stats), stats))
    scored.sort(key=lambda x: (-x[1], -x[2]["n"]))
    top = scored[:5]

    sess_count, sess_src, had_error = _current_session_count()
    sess_disp = f"{sess_count}" if sess_count is not None else f"unknown ({sess_src})"

    lines = ["================ ToolForge Status ================",
             f"Total approved installs:  {total}",
             f"Current session tool calls:  {sess_disp}",
             "",
             "Top 5 rated tools (Bayesian-shrunk decayed score, matches curator ranking):"]
    if not top:
        lines.append("  (no ratings yet)")
    else:
        for name, score, stats in top:
            raw = f"{stats['avg']:.2f}" if stats["avg"] is not None else "n/a"
            lines.append(
                f"  {name:30s}  score {score:.2f}  raw avg {raw}  ({stats['n']} rating(s))"
            )
    lines += ["", "Last 5 ratings:"]
    if not last:
        lines.append("  (no ratings yet)")
    else:
        for name, rating, when in last:
            lines.append(f"  {when}  {name:30s}  {rating}/5")
    lines.append("==================================================")
    return "\n".join(lines), had_error


def _self_test() -> int:
    # WARN: see SKETCHY_CODE_AUDIT.md#s5-3 — FIXED in F40 (stale env-var line removed from docstring).
    """Smoke test that exercises v1 + v2 schema against a temp DB.

    Does NOT touch ~/.claude/toolforge.db.
    """
    global DB_PATH
    saved = DB_PATH
    tmpdir = Path(tempfile.mkdtemp(prefix="toolforge_test_"))
    DB_PATH = tmpdir / "toolforge.db"
    passed = 0
    failed = 0
    try:
        # 1. init + schema version
        init_db()
        conn = _connect()
        try:
            v = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        if v == SCHEMA_VERSION:
            print(f"OK: schema migrated to v{v}")
            passed += 1
        else:
            print(f"FAIL: schema expected v{SCHEMA_VERSION}, got v{v}")
            failed += 1

        # 2. v1 path: log_install + log_rating + get_rating_stats
        log_install("alpha-tool", "ui", True)
        log_rating("alpha-tool", 5)
        log_rating("alpha-tool", 4)
        stats = get_rating_stats("alpha-tool")
        if stats["n"] == 2 and abs(stats["avg"] - 4.5) < 1e-6:
            print("OK: v1 ratings path")
            passed += 1
        else:
            print(f"FAIL: v1 ratings got {stats}")
            failed += 1

        # 3. v2 usage_stats roundtrip
        upsert_usage_stats("skill:gsap-react", 12, "2026-05-25T10:00:00Z")
        upsert_usage_stats("mcp:github", 3, "2026-05-25T11:00:00Z")
        bulk = get_usage_stats_bulk(["skill:gsap-react", "mcp:github", "skill:nonexistent"])
        if (bulk["skill:gsap-react"]["count_30d"] == 12
                and bulk["mcp:github"]["count_30d"] == 3
                and bulk["skill:nonexistent"]["count_30d"] == 0):
            print("OK: v2 usage_stats roundtrip")
            passed += 1
        else:
            print(f"FAIL: v2 usage_stats got {bulk}")
            failed += 1

        # 4. v2 usage_stats UPSERT replaces
        upsert_usage_stats("skill:gsap-react", 20, "2026-05-26T10:00:00Z")
        bulk2 = get_usage_stats_bulk(["skill:gsap-react"])
        if bulk2["skill:gsap-react"]["count_30d"] == 20:
            print("OK: v2 usage_stats upsert overwrites")
            passed += 1
        else:
            print(f"FAIL: upsert got {bulk2}")
            failed += 1

        # 5. v2 deprecations
        upsert_deprecation("https://github.com/foo/bar", "foo-bar", True, "2025-01-01T00:00:00Z")
        dep = get_deprecation("https://github.com/foo/bar")
        if dep and dep["archived"] is True and dep["tool_name"] == "foo-bar":
            print("OK: v2 deprecations roundtrip")
            passed += 1
        else:
            print(f"FAIL: deprecation got {dep}")
            failed += 1

        # 6. v3 pipelines roundtrip
        steps = '[{"step":1,"skill_name":"code-review","sub_task":"review it"}]'
        pid = save_pipeline("review the PR", "abc123", steps, True)
        found = get_pipelines_by_hash("abc123")
        recent = get_recent_pipelines(1)
        if (pid > 0 and found and found[0]["success"] is True
                and found[0]["task_desc"] == "review the PR"
                and recent and recent[0]["id"] == pid):
            print("OK: v3 pipelines roundtrip")
            passed += 1
        else:
            print(f"FAIL: pipelines got pid={pid}, found={found}, recent={recent}")
            failed += 1

        # 7. pipelines: invalid JSON rejected
        try:
            save_pipeline("task", "hash", "not-json", False)
            print("FAIL: invalid steps_json accepted")
            failed += 1
        except ValueError:
            print("OK: invalid steps_json rejected")
            passed += 1

        # 8. v2 routing_scores (was test 6 — renumbered after inserting pipeline tests)
        upsert_routing_score("skill:impeccable", 0.5, 0.7, 0.3, 0.8, 0.6)
        scores = get_routing_scores_bulk(["skill:impeccable", "skill:missing"])
        if (scores["skill:impeccable"]
                and abs(scores["skill:impeccable"]["composite"] - 0.6) < 1e-6
                and scores["skill:missing"] is None):
            print("OK: v2 routing_scores roundtrip")
            passed += 1
        else:
            print(f"FAIL: routing got {scores}")
            failed += 1

        # 7. validation: bad tool_key rejected
        try:
            upsert_usage_stats("invalid no colon", 1, None)
            print("FAIL: invalid tool_key accepted")
            failed += 1
        except ValueError:
            print("OK: invalid tool_key rejected")
            passed += 1

        # 8. validation: bad url rejected
        try:
            upsert_deprecation("not-a-url", "x", False, None)
            print("FAIL: invalid url accepted")
            failed += 1
        except ValueError:
            print("OK: invalid url rejected")
            passed += 1

        # 9. idempotency: init_db twice no error
        init_db()
        init_db()
        print("OK: init_db idempotent")
        passed += 1

    finally:
        DB_PATH = saved
        try:
            for p in tmpdir.iterdir():
                p.unlink()
            tmpdir.rmdir()
        except OSError:
            pass

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_db.py init\n"
        "  toolforge_db.py log_install <name> <category> <approved:0|1>\n"
        "  toolforge_db.py log_rating <name> <1-5>\n"
        "  toolforge_db.py get_avg_rating <name>\n"
        "  toolforge_db.py get_rating_stats <name>\n"
        "  toolforge_db.py get_rating_stats_bulk <n1> ...\n"
        "  toolforge_db.py rate_last <1-5>\n"
        "  toolforge_db.py status\n"
        "  toolforge_db.py upsert_usage <tool_key> <count_30d> [<last_used_at>]\n"
        "  toolforge_db.py get_usage_bulk <k1> ...\n"
        "  toolforge_db.py upsert_deprecation <url> <tool_name> <archived:0|1> [<last_push_at>]\n"
        "  toolforge_db.py get_deprecation <url>\n"
        "  toolforge_db.py upsert_routing <tool_key> <desc> <name> <usage> <likert> <composite>\n"
        "  toolforge_db.py get_routing_bulk <k1> ...\n"
        "  toolforge_db.py save_pipeline <task_desc> <steps_hash> <steps_json> [--success]\n"
        "  toolforge_db.py get_pipelines_by_hash <steps_hash> [<limit>]\n"
        "  toolforge_db.py get_recent_pipelines [<limit>]\n"
        "  toolforge_db.py schema_version\n"
        "  toolforge_db.py --self-test\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2

    cmd = argv[1]
    try:
        if cmd == "init":
            init_db()
            print("ok")
            return 0
        if cmd == "log_install":
            if argv[4] not in {"0", "1", "true", "false", "True", "False"}:
                raise ValueError("invalid approved flag: must be 0 or 1")
            log_install(argv[2], argv[3], argv[4] in {"1", "true", "True"})
            print("ok")
            return 0
        if cmd == "log_rating":
            log_rating(argv[2], int(argv[3]))
            print("ok")
            return 0
        if cmd == "get_avg_rating":
            v = get_avg_rating(argv[2])
            print("null" if v is None else f"{v:.2f}")
            return 0
        if cmd == "get_rating_stats":
            print(json.dumps(get_rating_stats(argv[2])))
            return 0
        if cmd == "get_rating_stats_bulk":
            print(json.dumps(get_rating_stats_bulk(list(argv[2:]))))
            return 0
        if cmd == "rate_last":
            tool, msg, code = rate_last(int(argv[2]))
            print(msg)
            return code
        if cmd == "status":
            out, had_error = status()
            print(out)
            if had_error:
                return 3
            return 0
        if cmd == "upsert_usage":
            last_used = argv[4] if len(argv) > 4 else None
            upsert_usage_stats(argv[2], int(argv[3]), last_used)
            print("ok")
            return 0
        if cmd == "get_usage_bulk":
            print(json.dumps(get_usage_stats_bulk(list(argv[2:]))))
            return 0
        if cmd == "upsert_deprecation":
            last_push = argv[5] if len(argv) > 5 else None
            archived_flag = argv[4] in {"1", "true", "True"}
            upsert_deprecation(argv[2], argv[3], archived_flag, last_push)
            print("ok")
            return 0
        if cmd == "get_deprecation":
            out = get_deprecation(argv[2])
            print(json.dumps(out))
            return 0
        if cmd == "upsert_routing":
            upsert_routing_score(
                argv[2], float(argv[3]), float(argv[4]), float(argv[5]),
                float(argv[6]), float(argv[7]),
            )
            print("ok")
            return 0
        if cmd == "get_routing_bulk":
            print(json.dumps(get_routing_scores_bulk(list(argv[2:]))))
            return 0
        if cmd == "save_pipeline":
            # save_pipeline <task_desc> <steps_hash> <steps_json> [--success]
            success_flag = "--success" in argv
            args = [a for a in argv[2:] if a != "--success"]
            row_id = save_pipeline(args[0], args[1], args[2], success_flag)
            print(row_id)
            return 0
        if cmd == "get_pipelines_by_hash":
            limit = int(argv[3]) if len(argv) > 3 else 5
            print(json.dumps(get_pipelines_by_hash(argv[2], limit)))
            return 0
        if cmd == "get_recent_pipelines":
            limit = int(argv[2]) if len(argv) > 2 else 10
            print(json.dumps(get_recent_pipelines(limit)))
            return 0
        if cmd == "schema_version":
            init_db()
            conn = _connect()
            try:
                v = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            print(v)
            return 0
        if cmd == "--self-test":
            return _self_test()
    except sqlite3.Error as exc:
        print(f"toolforge_db sqlite error: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"toolforge_db os error: {exc}", file=sys.stderr)
        return 3
    except (IndexError, ValueError) as exc:
        print(f"toolforge_db error: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2

    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
