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
SCHEMA_VERSION = 8
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
    # timeout (Python-level retry loop) and busy_timeout (SQLite-level) cover
    # different wait paths; keep both, aligned at 10s for multi-writer (webui+bridge+hooks).
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
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

# Path-keyed guard: full DDL runs once per resolved DB path. Keyed (not a bare
# bool) because _self_test swaps DB_PATH to a tmp file — the guard must not leak
# the real-DB entry onto the tmp path or vice versa.
_initialized: dict[str, bool] = {}


def _exec_ddl(conn: sqlite3.Connection, script: str) -> None:
    """Run a multi-statement DDL block inside the current transaction.

    Unlike conn.executescript(), this does NOT issue an implicit COMMIT, so the
    BEGIN IMMEDIATE write lock held by init_db survives across all migration blocks.
    Statements are split with sqlite3.complete_statement, which respects
    semicolons inside string literals and trigger bodies (a naive split breaks
    the day the schema gains a CREATE TRIGGER ... BEGIN ... END block).
    """
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt and stmt != ";":
                conn.execute(stmt)
            buf = ""
    stmt = buf.strip().rstrip(";").strip()
    if stmt:
        conn.execute(stmt)


def init_db(force: bool = False) -> None:
    # resolve() is fine on a not-yet-existing path; an existence-conditional key
    # would change between the first call (parent missing) and the second
    # (parent created by _connect), silently defeating the guard.
    key = str(DB_PATH.resolve())
    if not force and _initialized.get(key):
        return
    conn = _connect()
    try:
        # BEGIN IMMEDIATE acquires the write lock up front, so two processes can't
        # both observe user_version==0 and both seed (F-race). executescript()
        # issues an implicit COMMIT that would drop that lock, so DDL goes through
        # _exec_ddl (statement-by-statement, no auto-commit) and we read
        # user_version *after* the lock is held. Migration blocks stay idempotent
        # (CREATE TABLE IF NOT EXISTS, current<N gating); seeding still fires only
        # when current==0, now decided atomically under the lock.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        _exec_ddl(
            conn,
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
            """,
        )
        if current < 2:
            _exec_ddl(
                conn,
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
            _exec_ddl(
                conn,
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
        if current < 5:
            _exec_ddl(
                conn,
                """
                CREATE TABLE IF NOT EXISTS token_stats (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id      TEXT NOT NULL,
                    skill_name      TEXT,
                    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
                    output_tokens   INTEGER NOT NULL DEFAULT 0,
                    total_tokens    INTEGER NOT NULL DEFAULT 0,
                    recorded_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_token_stats_skill
                    ON token_stats(skill_name);
                CREATE INDEX IF NOT EXISTS idx_token_stats_session
                    ON token_stats(session_id);

                CREATE TABLE IF NOT EXISTS predictions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id      TEXT NOT NULL,
                    predicted_skill TEXT NOT NULL,
                    confidence      REAL NOT NULL DEFAULT 0.0,
                    was_used        INTEGER NOT NULL DEFAULT 0,
                    predicted_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_predictions_session
                    ON predictions(session_id);
                CREATE INDEX IF NOT EXISTS idx_predictions_skill
                    ON predictions(predicted_skill);

                CREATE TABLE IF NOT EXISTS skill_stacks (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    stack_name      TEXT NOT NULL UNIQUE,
                    display_name    TEXT NOT NULL,
                    description     TEXT,
                    skills_json     TEXT NOT NULL DEFAULT '[]',
                    org_id          TEXT,
                    is_builtin      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_stacks_org ON skill_stacks(org_id);

                CREATE TABLE IF NOT EXISTS org_profiles (
                    org_id          TEXT PRIMARY KEY,
                    org_name        TEXT NOT NULL,
                    admin_email     TEXT,
                    shared_catalog  INTEGER NOT NULL DEFAULT 1,
                    config_json     TEXT NOT NULL DEFAULT '{}',
                    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );

                CREATE TABLE IF NOT EXISTS skill_performance (
                    skill_name      TEXT PRIMARY KEY,
                    avg_latency_ms  REAL NOT NULL DEFAULT 0.0,
                    p95_latency_ms  REAL NOT NULL DEFAULT 0.0,
                    error_count     INTEGER NOT NULL DEFAULT 0,
                    success_count   INTEGER NOT NULL DEFAULT 0,
                    token_avg       REAL NOT NULL DEFAULT 0.0,
                    last_measured   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                """
            )
        if current < 6:
            _exec_ddl(
                conn,
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type_id      TEXT NOT NULL,
                    skill_name        TEXT NOT NULL,
                    preference_score  REAL NOT NULL DEFAULT 0.0,
                    positive_signals  INTEGER NOT NULL DEFAULT 0,
                    negative_signals  INTEGER NOT NULL DEFAULT 0,
                    last_observed     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(task_type_id, skill_name)
                );
                CREATE INDEX IF NOT EXISTS idx_prefs_task
                    ON user_preferences(task_type_id);
                CREATE INDEX IF NOT EXISTS idx_prefs_skill
                    ON user_preferences(skill_name);

                CREATE TABLE IF NOT EXISTS workflow_shortcuts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    shortcut_name   TEXT UNIQUE,
                    description     TEXT,
                    trigger_skills  TEXT NOT NULL DEFAULT '[]',
                    steps_json      TEXT NOT NULL DEFAULT '[]',
                    hit_count       INTEGER NOT NULL DEFAULT 0,
                    auto_detected   INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    last_triggered  TEXT
                );

                CREATE TABLE IF NOT EXISTS context_sync (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    integration   TEXT NOT NULL,
                    direction     TEXT NOT NULL,
                    payload_hash  TEXT,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    error_msg     TEXT,
                    synced_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_sync_int
                    ON context_sync(integration, synced_at DESC);
                """
            )
        if current < 7:
            _exec_ddl(
                conn,
                """
                CREATE TABLE IF NOT EXISTS skill_versions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_name      TEXT NOT NULL,
                    generation      INTEGER NOT NULL DEFAULT 1,
                    skill_md_path   TEXT NOT NULL,
                    skill_md_backup TEXT NOT NULL,
                    proposal        TEXT,
                    outcome         TEXT NOT NULL DEFAULT 'pending'
                        CHECK (outcome IN ('pending','improved','kept','discarded','rolled_back')),
                    baseline_score  REAL,
                    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_skill_versions_name
                    ON skill_versions(skill_name, id DESC);
                """
            )
        if current < 8:
            # ALTER TABLE ADD COLUMN is not idempotent (errors if the column already
            # exists), but the current<8 gate runs this block at most once during the
            # upgrade, under the IMMEDIATE write lock — same guarantee every prior
            # block relies on. Columns are nullable so existing v7 rows stay valid.
            _exec_ddl(
                conn,
                """
                ALTER TABLE skill_versions ADD COLUMN parent_generation INTEGER;
                ALTER TABLE skill_versions ADD COLUMN eval_score REAL;

                CREATE TABLE IF NOT EXISTS skill_frontier (
                    skill_name  TEXT PRIMARY KEY,
                    generation  INTEGER NOT NULL,
                    score       REAL NOT NULL,
                    updated_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scorer_results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_name  TEXT NOT NULL,
                    scorer      TEXT NOT NULL,
                    score       REAL NOT NULL,
                    detail      TEXT,
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scorer_results_name_scorer
                    ON scorer_results(skill_name, scorer);
                """
            )
        if current < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    # Seed catalog on brand-new installs only (not on upgrades — existing users
    # already have real rating data that should not be polluted with synthetic rows).
    # `current` was read under the IMMEDIATE lock, so exactly one process sees 0.
    if current == 0:
        try:
            import toolforge_catalog  # type: ignore
            toolforge_catalog.seed_db()
        except Exception:
            pass  # catalog seeding is best-effort; never block init

    _initialized[key] = True


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


# ---------- section: token-stats-v5 ----------


def log_token_stats(session_id: str, skill_name: Optional[str],
                    prompt_tokens: int, output_tokens: int) -> None:
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO token_stats
               (session_id, skill_name, prompt_tokens, output_tokens, total_tokens)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, skill_name, prompt_tokens, output_tokens, prompt_tokens + output_tokens),
        )
        conn.commit()
    finally:
        conn.close()


def get_token_stats_bulk(skill_names: list[str]) -> dict:
    init_db()
    if not skill_names:
        return {}
    placeholders = ",".join("?" * len(skill_names))
    conn = _connect()
    try:
        rows = conn.execute(
            f"""SELECT skill_name,
                       COUNT(*) AS sessions,
                       AVG(total_tokens) AS avg_tokens,
                       MIN(total_tokens) AS min_tokens,
                       MAX(total_tokens) AS max_tokens
                FROM token_stats
                WHERE skill_name IN ({placeholders})
                GROUP BY skill_name""",
            skill_names,
        ).fetchall()
    finally:
        conn.close()
    return {
        row[0]: {"sessions": row[1], "avg_tokens": row[2],
                 "min_tokens": row[3], "max_tokens": row[4]}
        for row in rows
    }


def get_token_efficiency_rank() -> list[dict]:
    """Returns skills sorted by lowest avg_tokens (most token-efficient first)."""
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT skill_name, COUNT(*) AS sessions, AVG(total_tokens) AS avg_tokens
               FROM token_stats
               WHERE skill_name IS NOT NULL
               GROUP BY skill_name
               HAVING COUNT(*) >= 3
               ORDER BY avg_tokens ASC
               LIMIT 20"""
        ).fetchall()
    finally:
        conn.close()
    return [{"skill_name": r[0], "sessions": r[1], "avg_tokens": round(r[2], 1)} for r in rows]


# ---------- section: predictions-v5 ----------


def log_prediction(session_id: str, predicted_skill: str, confidence: float) -> None:
    init_db()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO predictions (session_id, predicted_skill, confidence) VALUES (?, ?, ?)",
            (session_id, predicted_skill, max(0.0, min(1.0, confidence))),
        )
        conn.commit()
    finally:
        conn.close()


def confirm_prediction(session_id: str, predicted_skill: str) -> None:
    init_db()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE predictions SET was_used=1 WHERE session_id=? AND predicted_skill=?",
            (session_id, predicted_skill),
        )
        conn.commit()
    finally:
        conn.close()


def get_prediction_accuracy() -> dict:
    """Overall hit-rate: predictions that were confirmed used / all predictions."""
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(was_used) AS hits,
                      AVG(confidence) AS avg_confidence
               FROM predictions"""
        ).fetchone()
    finally:
        conn.close()
    total, hits, avg_conf = row
    total = total or 0
    hits = hits or 0
    return {
        "total": total,
        "hits": hits,
        "accuracy": round(hits / total, 3) if total else None,
        "avg_confidence": round(avg_conf, 3) if avg_conf else None,
    }


def get_top_predicted_skills(limit: int = 10) -> list[dict]:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT predicted_skill,
                      COUNT(*) AS predictions,
                      SUM(was_used) AS hits,
                      AVG(confidence) AS avg_conf
               FROM predictions
               GROUP BY predicted_skill
               ORDER BY hits DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"skill": r[0], "predictions": r[1], "hits": r[2],
         "hit_rate": round(r[2] / r[1], 3) if r[1] else 0.0,
         "avg_confidence": round(r[3], 3) if r[3] else 0.0}
        for r in rows
    ]


# ---------- section: skill-stacks-v5 ----------


def save_skill_stack(stack_name: str, display_name: str, description: str,
                     skills: list[str], org_id: Optional[str] = None,
                     is_builtin: bool = False) -> None:
    if not stack_name or not re.match(r"^[a-z0-9._-]{1,60}$", stack_name):
        raise ValueError(f"invalid stack_name {stack_name!r}")
    skills_json = json.dumps(skills)
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO skill_stacks
               (stack_name, display_name, description, skills_json, org_id, is_builtin, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
               ON CONFLICT(stack_name) DO UPDATE SET
                 display_name=excluded.display_name,
                 description=excluded.description,
                 skills_json=excluded.skills_json,
                 org_id=excluded.org_id,
                 updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (stack_name, display_name, description, skills_json, org_id, 1 if is_builtin else 0),
        )
        conn.commit()
    finally:
        conn.close()


def get_skill_stack(stack_name: str) -> Optional[dict]:
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT stack_name, display_name, description, skills_json, org_id, is_builtin, created_at, updated_at FROM skill_stacks WHERE stack_name=?",
            (stack_name,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "stack_name": row[0], "display_name": row[1], "description": row[2],
        "skills": json.loads(row[3]), "org_id": row[4],
        "is_builtin": bool(row[5]), "created_at": row[6], "updated_at": row[7],
    }


def list_skill_stacks(org_id: Optional[str] = None) -> list[dict]:
    init_db()
    conn = _connect()
    try:
        if org_id:
            rows = conn.execute(
                "SELECT stack_name, display_name, description, skills_json, org_id, is_builtin FROM skill_stacks WHERE org_id=? OR is_builtin=1 ORDER BY is_builtin DESC, stack_name",
                (org_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT stack_name, display_name, description, skills_json, org_id, is_builtin FROM skill_stacks ORDER BY is_builtin DESC, stack_name"
            ).fetchall()
    finally:
        conn.close()
    return [
        {"stack_name": r[0], "display_name": r[1], "description": r[2],
         "skills": json.loads(r[3]), "org_id": r[4], "is_builtin": bool(r[5])}
        for r in rows
    ]


def delete_skill_stack(stack_name: str) -> bool:
    init_db()
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM skill_stacks WHERE stack_name=? AND is_builtin=0", (stack_name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------- section: org-profiles-v5 ----------


def save_org_profile(org_id: str, org_name: str, admin_email: Optional[str] = None,
                     shared_catalog: bool = True, config: Optional[dict] = None) -> None:
    if not org_id or not re.match(r"^[a-z0-9._-]{1,60}$", org_id):
        raise ValueError(f"invalid org_id {org_id!r}")
    config_json = json.dumps(config or {})
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO org_profiles
               (org_id, org_name, admin_email, shared_catalog, config_json, updated_at)
               VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
               ON CONFLICT(org_id) DO UPDATE SET
                 org_name=excluded.org_name,
                 admin_email=excluded.admin_email,
                 shared_catalog=excluded.shared_catalog,
                 config_json=excluded.config_json,
                 updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (org_id, org_name, admin_email, 1 if shared_catalog else 0, config_json),
        )
        conn.commit()
    finally:
        conn.close()


def get_org_profile(org_id: str) -> Optional[dict]:
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT org_id, org_name, admin_email, shared_catalog, config_json, created_at, updated_at FROM org_profiles WHERE org_id=?",
            (org_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "org_id": row[0], "org_name": row[1], "admin_email": row[2],
        "shared_catalog": bool(row[3]), "config": json.loads(row[4]),
        "created_at": row[5], "updated_at": row[6],
    }


def list_org_profiles() -> list[dict]:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT org_id, org_name, admin_email, shared_catalog FROM org_profiles ORDER BY org_name"
        ).fetchall()
    finally:
        conn.close()
    return [{"org_id": r[0], "org_name": r[1], "admin_email": r[2], "shared_catalog": bool(r[3])} for r in rows]


# ---------- section: skill-performance-v5 ----------


def upsert_skill_performance(skill_name: str, latency_ms: float,
                              success: bool, token_count: int = 0) -> None:
    name = _validate_tool_name(skill_name)
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT avg_latency_ms, error_count, success_count, token_avg FROM skill_performance WHERE skill_name=?",
            (name,),
        ).fetchone()
        if row:
            prev_avg, err_cnt, ok_cnt, prev_tok_avg = row
            total = err_cnt + ok_cnt + 1
            new_ok = ok_cnt + (1 if success else 0)
            new_err = err_cnt + (0 if success else 1)
            # Exponential moving average (α=0.2)
            new_avg_lat = prev_avg * 0.8 + latency_ms * 0.2
            new_tok_avg = prev_tok_avg * 0.8 + token_count * 0.2 if token_count > 0 else prev_tok_avg
            conn.execute(
                """UPDATE skill_performance SET
                   avg_latency_ms=?, error_count=?, success_count=?, token_avg=?,
                   last_measured=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE skill_name=?""",
                (new_avg_lat, new_err, new_ok, new_tok_avg, name),
            )
        else:
            conn.execute(
                """INSERT INTO skill_performance
                   (skill_name, avg_latency_ms, error_count, success_count, token_avg)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, latency_ms, 0 if success else 1, 1 if success else 0, float(token_count)),
            )
        conn.commit()
    finally:
        conn.close()


def get_skill_performance(skill_name: str) -> Optional[dict]:
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT skill_name, avg_latency_ms, p95_latency_ms, error_count, success_count, token_avg, last_measured FROM skill_performance WHERE skill_name=?",
            (_normalize(skill_name),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    total = row[3] + row[4]
    return {
        "skill_name": row[0], "avg_latency_ms": row[1], "p95_latency_ms": row[2],
        "error_count": row[3], "success_count": row[4],
        "error_rate": round(row[3] / total, 3) if total else 0.0,
        "token_avg": row[5], "last_measured": row[6],
    }


def get_skill_performance_all() -> list[dict]:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT skill_name, avg_latency_ms, error_count, success_count, token_avg
               FROM skill_performance ORDER BY success_count DESC"""
        ).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        total = r[2] + r[3]
        result.append({
            "skill_name": r[0], "avg_latency_ms": round(r[1], 1),
            "error_count": r[2], "success_count": r[3],
            "error_rate": round(r[2] / total, 3) if total else 0.0,
            "token_avg": round(r[4], 1),
        })
    return result


# ---------- section: user-preferences-v6 ----------


def record_preference_signal(task_type_id: str, skill_name: str,
                              positive: bool, weight: float = 1.0) -> None:
    """Record one positive or negative preference signal for a skill/task combination.

    Preference score uses an additive model with decay ceiling:
      positive: +0.15 * weight, capped at +2.0
      negative: -0.08 * weight, floored at -1.0
    """
    delta = 0.15 * weight if positive else -0.08 * weight
    name = _validate_tool_name(skill_name)
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO user_preferences
               (task_type_id, skill_name, preference_score, positive_signals, negative_signals)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(task_type_id, skill_name) DO UPDATE SET
                 preference_score = MAX(-1.0, MIN(2.0,
                     preference_score + ?)),
                 positive_signals = positive_signals + ?,
                 negative_signals = negative_signals + ?,
                 last_observed = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (task_type_id, name, delta,
             1 if positive else 0, 0 if positive else 1,
             delta, 1 if positive else 0, 0 if positive else 1),
        )
        conn.commit()
    finally:
        conn.close()


def get_preferences_for_task(task_type_id: str) -> list[dict]:
    """Return skills ranked by preference score for a task type (descending)."""
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT skill_name, preference_score, positive_signals, negative_signals
               FROM user_preferences
               WHERE task_type_id = ?
               ORDER BY preference_score DESC""",
            (task_type_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"skill_name": r[0], "preference_score": round(r[1], 3),
         "positive_signals": r[2], "negative_signals": r[3]}
        for r in rows
    ]


def get_all_preferences() -> dict[str, list[dict]]:
    """Return all preferences grouped by task_type_id."""
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT task_type_id, skill_name, preference_score
               FROM user_preferences
               ORDER BY task_type_id, preference_score DESC"""
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, list[dict]] = {}
    for task_id, skill, score in rows:
        result.setdefault(task_id, []).append(
            {"skill_name": skill, "preference_score": round(score, 3)}
        )
    return result


def get_preference_score(task_type_id: str, skill_name: str) -> float:
    """Return the preference score for a specific task/skill pair (0.0 if unknown)."""
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT preference_score FROM user_preferences WHERE task_type_id=? AND skill_name=?",
            (task_type_id, _normalize(skill_name)),
        ).fetchone()
    finally:
        conn.close()
    return round(row[0], 3) if row else 0.0


# ---------- section: workflow-shortcuts-v6 ----------


def save_workflow_shortcut(shortcut_name: str, description: str,
                           trigger_skills: list[str], steps: list[dict],
                           auto_detected: bool = True) -> None:
    if not shortcut_name or not re.match(r"^[a-z0-9._-]{1,60}$", shortcut_name):
        raise ValueError(f"invalid shortcut_name {shortcut_name!r}")
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workflow_shortcuts
               (shortcut_name, description, trigger_skills, steps_json, auto_detected)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(shortcut_name) DO UPDATE SET
                 description=excluded.description,
                 trigger_skills=excluded.trigger_skills,
                 steps_json=excluded.steps_json""",
            (shortcut_name, description, json.dumps(trigger_skills),
             json.dumps(steps), 1 if auto_detected else 0),
        )
        conn.commit()
    finally:
        conn.close()


def record_shortcut_trigger(shortcut_name: str) -> None:
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """UPDATE workflow_shortcuts SET
               hit_count = hit_count + 1,
               last_triggered = strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE shortcut_name=?""",
            (shortcut_name,),
        )
        conn.commit()
    finally:
        conn.close()


def list_workflow_shortcuts() -> list[dict]:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT shortcut_name, description, trigger_skills, steps_json,
                      hit_count, auto_detected, created_at, last_triggered
               FROM workflow_shortcuts ORDER BY hit_count DESC"""
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "shortcut_name": r[0], "description": r[1],
            "trigger_skills": json.loads(r[2]),
            "steps": json.loads(r[3]),
            "hit_count": r[4], "auto_detected": bool(r[5]),
            "created_at": r[6], "last_triggered": r[7],
        }
        for r in rows
    ]


def delete_workflow_shortcut(shortcut_name: str) -> bool:
    init_db()
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM workflow_shortcuts WHERE shortcut_name=?", (shortcut_name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------- section: context-sync-v6 ----------


def log_context_sync(integration: str, direction: str,
                     payload_hash: Optional[str] = None,
                     status: str = "ok", error_msg: Optional[str] = None) -> None:
    if integration not in {"hermes", "obsidian", "webhook", "generic"}:
        raise ValueError(f"unknown integration {integration!r}")
    if direction not in {"push", "pull"}:
        raise ValueError(f"direction must be 'push' or 'pull'")
    init_db()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO context_sync (integration, direction, payload_hash, status, error_msg) VALUES (?,?,?,?,?)",
            (integration, direction, payload_hash, status, error_msg),
        )
        conn.commit()
    finally:
        conn.close()


def get_sync_history(integration: Optional[str] = None, limit: int = 20) -> list[dict]:
    init_db()
    conn = _connect()
    try:
        if integration:
            rows = conn.execute(
                "SELECT integration, direction, status, error_msg, synced_at FROM context_sync WHERE integration=? ORDER BY synced_at DESC LIMIT ?",
                (integration, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT integration, direction, status, error_msg, synced_at FROM context_sync ORDER BY synced_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [
        {"integration": r[0], "direction": r[1], "status": r[2],
         "error_msg": r[3], "synced_at": r[4]}
        for r in rows
    ]


# ---------- section: skill-versions-v7 ----------

# Must stay in sync with the CHECK constraint on skill_versions.outcome.
SKILL_VERSION_OUTCOMES = {"pending", "improved", "kept", "discarded", "rolled_back"}


def save_skill_version(skill_name: str, path: str, backup_text: str,
                       proposal: Optional[str] = None,
                       baseline_score: Optional[float] = None) -> int:
    """Snapshot a skill's full SKILL.md text before an improve-pass rewrite.

    Returns the new row id. generation = 1 + max existing generation for that skill,
    computed and inserted in one connection so concurrent improves can't fork history.
    """
    name = _validate_tool_name(skill_name)
    if not path or not path.strip():
        raise ValueError("path must be non-empty")
    if not backup_text:
        raise ValueError("backup_text must be non-empty (nothing to roll back to)")
    init_db()
    conn = _connect()
    try:
        gen = conn.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 FROM skill_versions WHERE skill_name = ?",
            (name,),
        ).fetchone()[0]
        cur = conn.execute(
            """INSERT INTO skill_versions
               (skill_name, generation, skill_md_path, skill_md_backup, proposal, baseline_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, int(gen), path.strip(), backup_text, proposal,
             float(baseline_score) if baseline_score is not None else None),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_skill_versions(skill_name: str, limit: int = 10) -> list[dict]:
    """Return version snapshots for a skill, newest first."""
    if limit < 1 or limit > 100:
        raise ValueError("limit must be 1..100")
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT id, skill_name, generation, skill_md_path, skill_md_backup,
                      proposal, outcome, baseline_score, created_at
               FROM skill_versions
               WHERE skill_name = ?
               ORDER BY id DESC
               LIMIT ?""",
            (_normalize(skill_name), int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "skill_name": r[1], "generation": r[2],
         "skill_md_path": r[3], "skill_md_backup": r[4], "proposal": r[5],
         "outcome": r[6], "baseline_score": r[7], "created_at": r[8]}
        for r in rows
    ]


def set_skill_version_outcome(version_id: int, outcome: str) -> None:
    if outcome not in SKILL_VERSION_OUTCOMES:
        raise ValueError(
            f"invalid outcome {outcome!r}: must be one of {sorted(SKILL_VERSION_OUTCOMES)}"
        )
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE skill_versions SET outcome = ? WHERE id = ?",
            (outcome, int(version_id)),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no skill_version with id {version_id}")
    finally:
        conn.close()


# ---------- section: skill-frontier-v8 ----------


def get_frontier(skill_name: str) -> Optional[dict]:
    """Return the current best-known generation/score for a skill, or None."""
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT skill_name, generation, score, updated_at FROM skill_frontier WHERE skill_name=?",
            (_normalize(skill_name),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"skill_name": row[0], "generation": row[1], "score": row[2], "updated_at": row[3]}


def set_frontier(skill_name: str, generation: int, score: float) -> None:
    """Upsert the frontier (best-known version) for a skill. updated_at is set server-side."""
    name = _validate_tool_name(skill_name)
    init_db()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO skill_frontier (skill_name, generation, score, updated_at)
            VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(skill_name) DO UPDATE SET
                generation = excluded.generation,
                score      = excluded.score,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (name, int(generation), float(score)),
        )
        conn.commit()
    finally:
        conn.close()


def record_scorer_result(skill_name: str, scorer: str, score: float,
                         detail: Optional[str] = None) -> int:
    """Append one scorer result for a skill. Returns the new row id. Append-only."""
    name = _validate_tool_name(skill_name)
    s = scorer.strip()
    if not s:
        raise ValueError("scorer must be non-empty")
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO scorer_results (skill_name, scorer, score, detail, created_at)
               VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))""",
            (name, s, float(score), detail),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_scorer_results(skill_name: str, scorer: Optional[str] = None,
                       limit: int = 50) -> list[dict]:
    """Return scorer results for a skill, newest first. Optionally filter by scorer."""
    if limit < 1 or limit > 500:
        raise ValueError("limit must be 1..500")
    init_db()
    conn = _connect()
    try:
        if scorer is not None:
            rows = conn.execute(
                """SELECT id, skill_name, scorer, score, detail, created_at
                   FROM scorer_results
                   WHERE skill_name = ? AND scorer = ?
                   ORDER BY id DESC LIMIT ?""",
                (_normalize(skill_name), scorer.strip(), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, skill_name, scorer, score, detail, created_at
                   FROM scorer_results
                   WHERE skill_name = ?
                   ORDER BY id DESC LIMIT ?""",
                (_normalize(skill_name), int(limit)),
            ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "skill_name": r[1], "scorer": r[2], "score": r[3],
         "detail": r[4], "created_at": r[5]}
        for r in rows
    ]


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

        # 10. v7 schema present
        if SCHEMA_VERSION >= 7 and v >= 7:
            print("OK: v7 schema (skill_versions) active")
            passed += 1
        else:
            print(f"FAIL: expected schema >= 7, got SCHEMA_VERSION={SCHEMA_VERSION}, v={v}")
            failed += 1

        # 11. v7 skill_versions roundtrip: save -> outcome update -> list
        vid1 = save_skill_version("alpha-skill", "/tmp/skills/alpha-skill/SKILL.md",
                                  "# original v1\n", "tighten the trigger description", 2.4)
        vid2 = save_skill_version("alpha-skill", "/tmp/skills/alpha-skill/SKILL.md",
                                  "# original v2\n", "add examples section", 2.6)
        set_skill_version_outcome(vid1, "improved")
        versions = get_skill_versions("alpha-skill")
        if (vid1 > 0 and vid2 > vid1 and len(versions) == 2
                and versions[0]["id"] == vid2 and versions[0]["generation"] == 2
                and versions[0]["outcome"] == "pending"
                and versions[1]["id"] == vid1 and versions[1]["generation"] == 1
                and versions[1]["outcome"] == "improved"
                and abs(versions[1]["baseline_score"] - 2.4) < 1e-6
                and versions[1]["skill_md_backup"] == "# original v1\n"):
            print("OK: v7 skill_versions roundtrip")
            passed += 1
        else:
            print(f"FAIL: skill_versions got vid1={vid1}, vid2={vid2}, versions={versions}")
            failed += 1

        # 12. v7 invalid outcome rejected
        try:
            set_skill_version_outcome(vid2, "bogus")
            print("FAIL: invalid outcome accepted")
            failed += 1
        except ValueError:
            print("OK: invalid outcome rejected")
            passed += 1

        # 13. v8 schema present + new skill_versions columns nullable
        conn = _connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(skill_versions)").fetchall()}
        finally:
            conn.close()
        if SCHEMA_VERSION >= 8 and v >= 8 and "parent_generation" in cols and "eval_score" in cols:
            print("OK: v8 schema (skill_frontier, scorer_results, skill_versions.parent_generation/eval_score) active")
            passed += 1
        else:
            print(f"FAIL: expected schema >= 8 with new columns, got v={v}, cols={cols}")
            failed += 1

        # 14. v8 skill_frontier roundtrip + upsert overwrites
        if get_frontier("beta-skill") is None:
            set_frontier("beta-skill", 3, 0.71)
            f1 = get_frontier("beta-skill")
            set_frontier("beta-skill", 5, 0.88)
            f2 = get_frontier("beta-skill")
            if (f1 and f1["generation"] == 3 and abs(f1["score"] - 0.71) < 1e-6
                    and f2 and f2["generation"] == 5 and abs(f2["score"] - 0.88) < 1e-6
                    and f2["updated_at"]):
                print("OK: v8 skill_frontier roundtrip + upsert")
                passed += 1
            else:
                print(f"FAIL: skill_frontier got f1={f1}, f2={f2}")
                failed += 1
        else:
            print("FAIL: skill_frontier not empty at start")
            failed += 1

        # 15. v8 scorer_results roundtrip (append-only, scorer filter)
        sid1 = record_scorer_result("beta-skill", "trigger-eval", 0.6, "matched 3/5")
        sid2 = record_scorer_result("beta-skill", "trigger-eval", 0.9, None)
        record_scorer_result("beta-skill", "lint-eval", 0.4, "2 warnings")
        all_res = get_scorer_results("beta-skill")
        filtered = get_scorer_results("beta-skill", scorer="trigger-eval")
        if (sid2 > sid1 and len(all_res) == 3 and all_res[0]["scorer"] == "lint-eval"
                and len(filtered) == 2
                and all(r["scorer"] == "trigger-eval" for r in filtered)
                and filtered[0]["id"] == sid2 and abs(filtered[0]["score"] - 0.9) < 1e-6
                and filtered[1]["detail"] == "matched 3/5"):
            print("OK: v8 scorer_results roundtrip")
            passed += 1
        else:
            print(f"FAIL: scorer_results got sid1={sid1}, sid2={sid2}, all={all_res}, filtered={filtered}")
            failed += 1

        # 16. v5 skill_performance accessor roundtrip (write path confirmed for v8 callers)
        upsert_skill_performance("perf-skill", 120.0, True, 800)
        upsert_skill_performance("perf-skill", 80.0, False, 600)
        perf = get_skill_performance("perf-skill")
        if (perf and perf["success_count"] == 1 and perf["error_count"] == 1
                and abs(perf["error_rate"] - 0.5) < 1e-6 and perf["avg_latency_ms"] > 0):
            print("OK: v5 skill_performance accessor roundtrip")
            passed += 1
        else:
            print(f"FAIL: skill_performance got {perf}")
            failed += 1

        # 17. v8 invalid input rejected (bad skill name, empty scorer, bad limit)
        bad = 0
        try:
            set_frontier("BAD NAME WITH SPACES", 1, 0.5)
        except ValueError:
            bad += 1
        try:
            record_scorer_result("beta-skill", "   ", 0.5)
        except ValueError:
            bad += 1
        try:
            get_scorer_results("beta-skill", limit=0)
        except ValueError:
            bad += 1
        if bad == 3:
            print("OK: v8 invalid input rejected")
            passed += 1
        else:
            print(f"FAIL: v8 invalid input not all rejected ({bad}/3)")
            failed += 1

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
        "  toolforge_db.py log_token_stats <session_id> <skill_name|-> <prompt_tok> <output_tok>\n"
        "  toolforge_db.py get_token_stats_bulk <s1> ...\n"
        "  toolforge_db.py token_efficiency_rank\n"
        "  toolforge_db.py log_prediction <session_id> <skill> <confidence>\n"
        "  toolforge_db.py confirm_prediction <session_id> <skill>\n"
        "  toolforge_db.py prediction_accuracy\n"
        "  toolforge_db.py top_predicted_skills [<limit>]\n"
        "  toolforge_db.py save_stack <stack_name> <display_name> <description> <skills_json> [<org_id>] [--builtin]\n"
        "  toolforge_db.py get_stack <stack_name>\n"
        "  toolforge_db.py list_stacks [<org_id>]\n"
        "  toolforge_db.py delete_stack <stack_name>\n"
        "  toolforge_db.py save_org <org_id> <org_name> [<admin_email>]\n"
        "  toolforge_db.py get_org <org_id>\n"
        "  toolforge_db.py list_orgs\n"
        "  toolforge_db.py upsert_perf <skill_name> <latency_ms> <success:0|1> [<tokens>]\n"
        "  toolforge_db.py get_perf <skill_name>\n"
        "  toolforge_db.py perf_all\n"
        "  toolforge_db.py pref_signal <task_type_id> <skill_name> <positive:0|1> [<weight>]\n"
        "  toolforge_db.py get_prefs <task_type_id>\n"
        "  toolforge_db.py all_prefs\n"
        "  toolforge_db.py pref_score <task_type_id> <skill_name>\n"
        "  toolforge_db.py save_shortcut <name> <description> <trigger_skills_json> <steps_json> [--manual]\n"
        "  toolforge_db.py shortcut_trigger <name>\n"
        "  toolforge_db.py list_shortcuts\n"
        "  toolforge_db.py delete_shortcut <name>\n"
        "  toolforge_db.py log_sync <integration> <push|pull> [<status>] [<hash>]\n"
        "  toolforge_db.py sync_history [<integration>] [<limit>]\n"
        "  toolforge_db.py save_skill_version <skill> <skill_md_path> <backup_file> [<proposal>] [<baseline_score>]\n"
        "  toolforge_db.py get_skill_versions <skill> [<limit>]\n"
        "  toolforge_db.py set_version_outcome <version_id> <pending|improved|kept|discarded|rolled_back>\n"
        "  toolforge_db.py get_frontier <skill_name>\n"
        "  toolforge_db.py set_frontier <skill_name> <generation> <score>\n"
        "  toolforge_db.py record_scorer <skill_name> <scorer> <score> [<detail>]\n"
        "  toolforge_db.py get_scorer_results <skill_name> [<scorer>] [<limit>]\n"
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
        if cmd == "log_token_stats":
            skill = None if argv[3] == "-" else argv[3]
            log_token_stats(argv[2], skill, int(argv[4]), int(argv[5]))
            print("ok")
            return 0
        if cmd == "get_token_stats_bulk":
            print(json.dumps(get_token_stats_bulk(list(argv[2:]))))
            return 0
        if cmd == "token_efficiency_rank":
            print(json.dumps(get_token_efficiency_rank()))
            return 0
        if cmd == "log_prediction":
            log_prediction(argv[2], argv[3], float(argv[4]))
            print("ok")
            return 0
        if cmd == "confirm_prediction":
            confirm_prediction(argv[2], argv[3])
            print("ok")
            return 0
        if cmd == "prediction_accuracy":
            print(json.dumps(get_prediction_accuracy()))
            return 0
        if cmd == "top_predicted_skills":
            limit = int(argv[2]) if len(argv) > 2 else 10
            print(json.dumps(get_top_predicted_skills(limit)))
            return 0
        if cmd == "save_stack":
            org = argv[6] if len(argv) > 6 and argv[6] != "--builtin" else None
            builtin = "--builtin" in argv
            skills = json.loads(argv[5])
            save_skill_stack(argv[2], argv[3], argv[4], skills, org, builtin)
            print("ok")
            return 0
        if cmd == "get_stack":
            print(json.dumps(get_skill_stack(argv[2])))
            return 0
        if cmd == "list_stacks":
            org = argv[2] if len(argv) > 2 else None
            print(json.dumps(list_skill_stacks(org)))
            return 0
        if cmd == "delete_stack":
            ok = delete_skill_stack(argv[2])
            print("ok" if ok else "not found (or builtin)")
            return 0
        if cmd == "save_org":
            email = argv[4] if len(argv) > 4 else None
            save_org_profile(argv[2], argv[3], email)
            print("ok")
            return 0
        if cmd == "get_org":
            print(json.dumps(get_org_profile(argv[2])))
            return 0
        if cmd == "list_orgs":
            print(json.dumps(list_org_profiles()))
            return 0
        if cmd == "upsert_perf":
            tokens = int(argv[5]) if len(argv) > 5 else 0
            upsert_skill_performance(argv[2], float(argv[3]), argv[4] in {"1", "true", "True"}, tokens)
            print("ok")
            return 0
        if cmd == "get_perf":
            print(json.dumps(get_skill_performance(argv[2])))
            return 0
        if cmd == "perf_all":
            print(json.dumps(get_skill_performance_all()))
            return 0
        if cmd == "pref_signal":
            positive = argv[4] in {"1", "true", "True"}
            weight = float(argv[5]) if len(argv) > 5 else 1.0
            record_preference_signal(argv[2], argv[3], positive, weight)
            print("ok")
            return 0
        if cmd == "get_prefs":
            print(json.dumps(get_preferences_for_task(argv[2])))
            return 0
        if cmd == "all_prefs":
            print(json.dumps(get_all_preferences()))
            return 0
        if cmd == "pref_score":
            print(get_preference_score(argv[2], argv[3]))
            return 0
        if cmd == "save_shortcut":
            manual = "--manual" in argv
            args = [a for a in argv[2:] if a != "--manual"]
            save_workflow_shortcut(
                args[0], args[1],
                json.loads(args[2]), json.loads(args[3]),
                auto_detected=not manual,
            )
            print("ok")
            return 0
        if cmd == "shortcut_trigger":
            record_shortcut_trigger(argv[2])
            print("ok")
            return 0
        if cmd == "list_shortcuts":
            print(json.dumps(list_workflow_shortcuts()))
            return 0
        if cmd == "delete_shortcut":
            ok = delete_workflow_shortcut(argv[2])
            print("ok" if ok else "not found")
            return 0
        if cmd == "log_sync":
            status_arg = argv[4] if len(argv) > 4 else "ok"
            hash_arg = argv[5] if len(argv) > 5 else None
            log_context_sync(argv[2], argv[3], hash_arg, status_arg)
            print("ok")
            return 0
        if cmd == "sync_history":
            integration = argv[2] if len(argv) > 2 and not argv[2].isdigit() else None
            limit = int(argv[3] if len(argv) > 3 else argv[2]) if len(argv) > 2 else 20
            print(json.dumps(get_sync_history(integration, limit)))
            return 0
        if cmd == "save_skill_version":
            # Backup text comes from a file, not argv — full SKILL.md bodies don't
            # survive shell argument passing (newlines, size, quoting).
            backup_text = Path(argv[4]).read_text(encoding="utf-8")
            proposal = argv[5] if len(argv) > 5 else None
            baseline = float(argv[6]) if len(argv) > 6 else None
            print(save_skill_version(argv[2], argv[3], backup_text, proposal, baseline))
            return 0
        if cmd == "get_skill_versions":
            limit = int(argv[3]) if len(argv) > 3 else 10
            print(json.dumps(get_skill_versions(argv[2], limit)))
            return 0
        if cmd == "set_version_outcome":
            set_skill_version_outcome(int(argv[2]), argv[3])
            print("ok")
            return 0
        if cmd == "get_frontier":
            print(json.dumps(get_frontier(argv[2])))
            return 0
        if cmd == "set_frontier":
            set_frontier(argv[2], int(argv[3]), float(argv[4]))
            print("ok")
            return 0
        if cmd == "record_scorer":
            detail = argv[5] if len(argv) > 5 else None
            print(record_scorer_result(argv[2], argv[3], float(argv[4]), detail))
            return 0
        if cmd == "get_scorer_results":
            scorer = argv[3] if len(argv) > 3 and not argv[3].isdigit() else None
            # limit may sit at argv[3] (scorer omitted) or argv[4] (scorer present)
            limit_arg = None
            if len(argv) > 4 and argv[4].isdigit():
                limit_arg = argv[4]
            elif len(argv) > 3 and argv[3].isdigit():
                limit_arg = argv[3]
            limit = int(limit_arg) if limit_arg is not None else 50
            print(json.dumps(get_scorer_results(argv[2], scorer, limit)))
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
    # Register this instance under the canonical module name BEFORE helpers like
    # toolforge_catalog.seed_db() run `import toolforge_db`. Without this, that
    # import creates a SECOND module instance whose DB_PATH is the real path even
    # while _self_test has swapped this instance's DB_PATH to a temp file — the
    # seeding connect then leaks an empty zero-table toolforge.db at the real path.
    sys.modules.setdefault("toolforge_db", sys.modules[__name__])
    sys.exit(main(sys.argv))
