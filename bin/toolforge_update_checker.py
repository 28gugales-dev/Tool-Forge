#!/usr/bin/env python3
"""ToolForge update checker. Passive health scan for installed tools.

Reads the local DB (no network) and flags tools that show health signals:
  stale     — no usage in 90 days (from usage_stats.last_used_at)
  archived  — GitHub repo marked archived (from deprecations.archived)
  inactive  — repo last pushed >90 days ago (from deprecations.last_push_at)
  low-rated — Bayesian Likert avg < 2.5 with n >= 3 user ratings
  dormant   — installed but never appeared in usage_stats (count_30d == 0)

Output written to ~/.claude/toolforge_health.json for toolforge-status to read.

The "better alternative" signal is NOT computed here — it requires live web
discovery which is expensive. Run /toolforge <category> to see fresh rankings.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
DB_PATH = CLAUDE_HOME / "toolforge.db"
HEALTH_CACHE = CLAUDE_HOME / "toolforge_health.json"
HEALTH_CACHE_TTL = 6 * 3600  # 6-hour TTL — re-check twice daily max

STALE_DAYS = 90
INACTIVE_DAYS = 90
LOW_RATING_THRESHOLD = 2.5
LOW_RATING_MIN_N = 3
BAYES_PRIOR_MEAN = 3.0
BAYES_PRIOR_WEIGHT = 5.0
DECAY_HALFLIFE_DAYS = 75.0


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"toolforge.db not found at {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _bayesian_avg(ratings: list[tuple[int, float]]) -> float:
    """Bayesian-shrunk Likert average with 75-day exponential decay."""
    if not ratings:
        return BAYES_PRIOR_MEAN
    weights = [math.exp(-max(0.0, age) / DECAY_HALFLIFE_DAYS) for _, age in ratings]
    wsum = sum(weights)
    if wsum < 1e-30:
        return BAYES_PRIOR_MEAN
    weighted_sum = sum(r * w for (r, _), w in zip(ratings, weights))
    n_eff = wsum
    return (weighted_sum + BAYES_PRIOR_MEAN * BAYES_PRIOR_WEIGHT) / (n_eff + BAYES_PRIOR_WEIGHT)


def _days_ago(iso_str: str | None) -> float | None:
    """Convert ISO timestamp to days since then. None if missing or unparseable."""
    if not iso_str:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


def check_health() -> dict:
    """Scan DB for tool health signals. Returns structured health report."""
    try:
        conn = _connect()
    except FileNotFoundError:
        return {"error": "no_db", "flags": [], "checked_at": _now()}
    except sqlite3.Error as exc:
        return {"error": f"db_error: {exc}", "flags": [], "checked_at": _now()}

    try:
        # --- installed tools ---
        installs = conn.execute(
            """
            SELECT DISTINCT tool_name, category,
                   MAX(installed_at) AS last_installed_at
            FROM installs
            WHERE approved = 1
            GROUP BY tool_name, category
            """
        ).fetchall()

        if not installs:
            return {"flags": [], "summary": "no tools installed", "checked_at": _now()}

        installed_names = [row[0] for row in installs]
        ph = ",".join("?" * len(installed_names))

        # --- usage stats ---
        usage_rows = conn.execute(
            f"SELECT tool_key, count_30d, last_used_at FROM usage_stats "
            f"WHERE tool_key IN ({','.join('?' * len(installed_names))})",
            [f"skill:{n}" for n in installed_names],
        ).fetchall()
        # Also check with plain tool_name (not namespace-prefixed) for non-skill entries
        usage_rows += conn.execute(
            f"SELECT tool_key, count_30d, last_used_at FROM usage_stats "
            f"WHERE tool_key IN ({ph})",
            installed_names,
        ).fetchall()

        usage: dict[str, dict] = {}
        for key, count, last_used in usage_rows:
            # Strip "skill:" prefix for lookup
            name = key.removeprefix("skill:")
            if name not in usage or (count or 0) > usage[name].get("count_30d", 0):
                usage[name] = {"count_30d": count or 0, "last_used_at": last_used}

        # --- ratings ---
        # Fetch rated_at and compute age in Python: stored timestamps are
        # millisecond ISO with a trailing 'Z' (e.g. ...000Z), which SQLite's
        # julianday(...,'utc') returns NULL for — that NULL became age 0 and
        # made ancient ratings look brand-new, skewing low-rated detection.
        rating_rows = conn.execute(
            f"""
            SELECT tool_name, rating, rated_at
            FROM ratings WHERE tool_name IN ({ph})
            """,
            installed_names,
        ).fetchall()
        ratings: dict[str, list[tuple[int, float]]] = {}
        for name, r, rated_at in rating_rows:
            age = _days_ago(rated_at)
            ratings.setdefault(name, []).append((int(r), age if age is not None else 0.0))

        # --- deprecations ---
        dep_rows = conn.execute(
            f"""
            SELECT tool_name, archived, last_push_at
            FROM deprecations WHERE tool_name IN ({ph})
            """,
            installed_names,
        ).fetchall()
        deps: dict[str, dict] = {
            name: {"archived": bool(arch), "last_push_at": push}
            for name, arch, push in dep_rows
        }

    except sqlite3.Error as exc:
        # Empty or partially-initialized DB (e.g. a leaked zero-table file):
        # health is simply unknown, never a crash.
        return {"error": f"db_error: {exc}", "flags": [], "checked_at": _now()}
    finally:
        conn.close()

    flags: list[dict] = []
    now_ts = time.time()

    for tool_name, category, installed_at in installs:
        tool_flags: list[str] = []
        details: dict = {}

        stats = usage.get(tool_name, {})
        count_30d = stats.get("count_30d", 0) or 0
        last_used_at = stats.get("last_used_at")

        # dormant: never appeared in usage_stats (never used via tool_use)
        if tool_name not in usage:
            tool_flags.append("dormant")

        # stale: last_used > STALE_DAYS ago
        if last_used_at:
            days_since_used = _days_ago(last_used_at)
            if days_since_used is not None and days_since_used > STALE_DAYS:
                tool_flags.append("stale")
                details["days_since_used"] = round(days_since_used, 1)

        # low-rated: Bayesian Likert < threshold with enough ratings
        tool_ratings = ratings.get(tool_name, [])
        if len(tool_ratings) >= LOW_RATING_MIN_N:
            bayes = _bayesian_avg(tool_ratings)
            if bayes < LOW_RATING_THRESHOLD:
                tool_flags.append("low-rated")
                details["bayesian_avg"] = round(bayes, 2)
                details["n_ratings"] = len(tool_ratings)

        # archived / inactive from deprecations
        dep = deps.get(tool_name, {})
        if dep.get("archived"):
            tool_flags.append("archived")
        else:
            last_push_days = _days_ago(dep.get("last_push_at"))
            if last_push_days is not None and last_push_days > INACTIVE_DAYS:
                tool_flags.append("inactive")
                details["days_since_push"] = round(last_push_days, 1)

        if tool_flags:
            flags.append({
                "tool_name": tool_name,
                "category": category,
                "flags": tool_flags,
                "details": details,
                "installed_at": installed_at,
            })

    # Summary line
    flag_counts: dict[str, int] = {}
    for entry in flags:
        for f in entry["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1

    parts = []
    if flag_counts.get("archived"):
        parts.append(f"{flag_counts['archived']} archived")
    if flag_counts.get("inactive"):
        parts.append(f"{flag_counts['inactive']} inactive")
    if flag_counts.get("low-rated"):
        parts.append(f"{flag_counts['low-rated']} low-rated")
    if flag_counts.get("stale"):
        parts.append(f"{flag_counts['stale']} stale")
    if flag_counts.get("dormant"):
        parts.append(f"{flag_counts['dormant']} dormant")

    summary = (", ".join(parts) + " (run /toolforge-status for details)") if parts else "all clear"

    return {
        "flags": flags,
        "flag_counts": flag_counts,
        "summary": summary,
        "total_installed": len(installs),
        "checked_at": _now(),
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_cached_health() -> dict | None:
    """Load cached health report if fresh (< TTL). Returns None if stale/missing."""
    try:
        if not HEALTH_CACHE.exists():
            return None
        age = time.time() - HEALTH_CACHE.stat().st_mtime
        if age > HEALTH_CACHE_TTL:
            return None
        return json.loads(HEALTH_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_health_cache(report: dict) -> None:
    try:
        tmp = HEALTH_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(tmp, HEALTH_CACHE)
    except OSError:
        pass


def _render_report(report: dict) -> str:
    """Format health report for /toolforge-status output."""
    flags = report.get("flags", [])
    total = report.get("total_installed", 0)
    summary = report.get("summary", "")
    checked = report.get("checked_at", "")

    if not flags:
        return f"  Health: {summary} ({total} tool(s) installed)"

    lines = [f"  Health: {summary}", f"  Checked: {checked}", ""]
    for entry in flags:
        flag_str = ", ".join(entry["flags"])
        details = entry.get("details", {})
        detail_str = ""
        if "days_since_used" in details:
            detail_str += f"  (last used {details['days_since_used']:.0f}d ago)"
        if "bayesian_avg" in details:
            detail_str += f"  (avg rating {details['bayesian_avg']:.1f}/5 from {details['n_ratings']} ratings)"
        if "days_since_push" in details:
            detail_str += f"  (repo silent {details['days_since_push']:.0f}d)"
        lines.append(f"  [{flag_str}] {entry['tool_name']}{detail_str}")

    lines.append("")
    lines.append("  Tip: run /toolforge <category> to find better alternatives.")
    return "\n".join(lines)


# ---------- self-test ----------

def _self_test() -> int:
    passed = failed = 0

    # _days_ago
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _days_ago(recent) is not None and 9 < _days_ago(recent) < 11
    assert _days_ago(old) is not None and 99 < _days_ago(old) < 101
    assert _days_ago(None) is None
    assert _days_ago("not-a-date") is None
    print("OK: _days_ago")
    passed += 1

    # _bayesian_avg — zero ratings returns prior
    avg = _bayesian_avg([])
    assert abs(avg - BAYES_PRIOR_MEAN) < 0.01, f"empty ratings avg={avg}"
    print("OK: _bayesian_avg empty -> prior")
    passed += 1

    # _bayesian_avg — 10 ratings all 5 should approach 5 but be shrunk
    avg = _bayesian_avg([(5, 0.0)] * 10)
    assert 4.0 < avg < 5.0, f"avg={avg} not in (4,5)"
    print("OK: _bayesian_avg 10x5 -> shrunk toward 5")
    passed += 1

    # _bayesian_avg — 1 rating of 1 should be shrunk toward 3 (prior)
    avg = _bayesian_avg([(1, 0.0)] * 1)
    assert 1.5 < avg < 3.0, f"avg={avg} not shrunk toward prior"
    print("OK: _bayesian_avg 1x1 -> shrunk toward prior")
    passed += 1

    # check_health with no DB returns error dict, not exception
    try:
        orig_db = HEALTH_CACHE
        result = check_health()
        if "error" in result or "flags" in result:
            print(f"OK: check_health returns dict (summary={result.get('summary', result.get('error'))})")
            passed += 1
        else:
            print(f"FAIL: unexpected check_health result: {result}")
            failed += 1
    except Exception as exc:
        print(f"FAIL: check_health raised: {exc}")
        failed += 1

    # _render_report with empty flags
    report = {"flags": [], "summary": "all clear", "total_installed": 3, "checked_at": _now()}
    rendered = _render_report(report)
    assert "all clear" in rendered and "3 tool" in rendered
    print("OK: _render_report empty flags")
    passed += 1

    # _render_report with a flagged tool
    report = {
        "flags": [{"tool_name": "old-mcp", "category": "ui", "flags": ["stale", "dormant"],
                   "details": {"days_since_used": 95.0}, "installed_at": "2026-01-01"}],
        "summary": "1 stale, 1 dormant",
        "total_installed": 5,
        "checked_at": _now(),
    }
    rendered = _render_report(report)
    assert "old-mcp" in rendered and "stale" in rendered and "95" in rendered
    print("OK: _render_report flagged tool")
    passed += 1

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- cli ----------

def _usage_str() -> str:
    return (
        "Usage:\n"
        "  toolforge_update_checker.py check [--force] [--json]\n"
        "  toolforge_update_checker.py render\n"
        "  toolforge_update_checker.py --self-test\n"
    )


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage_str(), file=sys.stderr)
        return 2
    if argv[0] == "--self-test":
        return _self_test()

    if argv[0] == "render":
        cached = load_cached_health()
        if cached is None:
            cached = check_health()
            save_health_cache(cached)
        print(_render_report(cached))
        return 0

    if argv[0] != "check":
        print(_usage_str(), file=sys.stderr)
        return 2

    force = "--force" in argv
    as_json = "--json" in argv

    cached = None if force else load_cached_health()
    if cached is None:
        cached = check_health()
        save_health_cache(cached)

    if as_json:
        print(json.dumps(cached, indent=2))
    else:
        print(_render_report(cached))

    n_flags = len(cached.get("flags", []))
    return 1 if n_flags > 0 else 0  # exit 1 = flags found (useful for CI/cron)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
