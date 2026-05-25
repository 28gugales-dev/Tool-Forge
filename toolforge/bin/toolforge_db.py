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

DB_PATH = Path(os.path.expanduser("~/.claude/toolforge.db"))
DECAY_HALFLIFE_DAYS = 180.0
SCHEMA_VERSION = 1
BAYES_PRIOR_MEAN = 3.0
BAYES_PRIOR_WEIGHT = 5.0

TOOL_NAME_RE = re.compile(r"^[a-z0-9._@/-]{1,80}$")
CATEGORY_RE = re.compile(r"^[a-z]{1,32}$")

_session_count_had_error = False


def _validate_category(cat: str) -> str:
    if not CATEGORY_RE.match(cat):
        raise ValueError(f"invalid category {cat!r}: must match {CATEGORY_RE.pattern}")
    return cat


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _normalize(name: str) -> str:
    return name.strip().lower()


def _validate_tool_name(name: str) -> str:
    n = _normalize(name)
    if not TOOL_NAME_RE.match(n):
        raise ValueError(f"invalid tool_name {name!r}: must match {TOOL_NAME_RE.pattern}")
    return n


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
        if current < SCHEMA_VERSION:
            # Future migrations chain here: if current < 2: ... ; if current < 3: ... ;
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        conn.commit()
    finally:
        conn.close()


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
    """Single-connection, single-query bulk lookup. O(1) round trips, not O(N)."""
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


def _current_session_count() -> tuple[Optional[int], str]:
    """Returns (count, source_label). Count is None when session id unknown."""
    global _session_count_had_error
    sid = os.environ.get("CLAUDE_SESSION_ID")
    if not sid:
        return None, "no CLAUDE_SESSION_ID in env"
    path = _session_counter_path(sid)
    try:
        return (path.stat().st_size if path.exists() else 0), str(path)
    except OSError as exc:
        _session_count_had_error = True
        return None, f"stat failed: {exc}"


def _shrunk_score(stats: dict) -> float:
    """Bayesian-shrunk posterior on decayed_avg. Same formula as curator skill."""
    n = stats["n"]
    if n == 0:
        return BAYES_PRIOR_MEAN
    return (stats["decayed_avg"] * n + BAYES_PRIOR_MEAN * BAYES_PRIOR_WEIGHT) / (n + BAYES_PRIOR_WEIGHT)


def status() -> str:
    init_db()
    conn = _connect()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM installs WHERE approved = 1"
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT tool_name, rating,
                   julianday('now') - julianday(rated_at, 'utc') AS age_days
            FROM ratings
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

    sess_count, sess_src = _current_session_count()
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
    return "\n".join(lines)


def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_db.py init\n"
        "  toolforge_db.py log_install <name> <category> <approved:0|1>\n"
        "  toolforge_db.py log_rating <name> <1-5>\n"
        "  toolforge_db.py get_avg_rating <name>           # prints 'null' or float\n"
        "  toolforge_db.py get_rating_stats <name>         # prints JSON dict\n"
        "  toolforge_db.py get_rating_stats_bulk <n1> ...  # prints JSON map\n"
        "  toolforge_db.py rate_last <1-5>\n"
        "  toolforge_db.py status\n"
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
            out = status()
            print(out)
            if _session_count_had_error:
                return 3
            return 0
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
