#!/usr/bin/env python3
"""ToolForge admin CLI. Provides manual management operations that would
otherwise require direct SQL:

  - Force-retire a skill (mark as low-rated without waiting for user feedback)
  - Override a tool's composite score
  - Create and manage skill stacks
  - View and manage org profiles
  - Trigger self-management routines (purge stale, rebalance scores)
  - View full system health dashboard

Intended for team admins and power users. All changes are written to the
shared SQLite DB and take effect on the next routing/discovery cycle.

CLI exit codes: 0 success, 1 not found, 2 usage error, 3 I/O error.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import toolforge_db as db

_SKILL_DIR = Path(__file__).parent.parent / "skills"
_PACKAGES_SCRIPT = Path(__file__).parent / "toolforge_packages.py"


# ---------- skill management ----------

def force_retire(tool_name: str, reason: str = "admin-retired") -> None:
    """Insert several low ratings to push a tool below the auto-retire threshold."""
    for _ in range(5):
        db.log_rating(tool_name, 1)
    print(f"  Retired {tool_name!r}: 5x 1-star ratings inserted. Reason: {reason}")


def override_rating(tool_name: str, rating: float) -> None:
    """Insert synthetic ratings to push a tool to a specific score."""
    target = max(1.0, min(5.0, rating))
    rounded = round(target)
    for _ in range(3):
        db.log_rating(tool_name, rounded)
    print(f"  Score override for {tool_name!r}: 3x {rounded}-star ratings inserted -> target ~{target:.1f}")


def reset_ratings(tool_name: str) -> None:
    """Remove all ratings for a tool (admin wipe — use with care)."""
    db.init_db()
    conn = db._connect()
    try:
        rows = conn.execute("DELETE FROM ratings WHERE tool_name=?", (tool_name,))
        conn.commit()
        print(f"  Deleted {rows.rowcount} ratings for {tool_name!r}")
    finally:
        conn.close()


# ---------- stack management ----------

def create_stack(stack_name: str, display_name: str,
                 description: str, skills: list[str],
                 org_id: str | None = None) -> None:
    db.save_skill_stack(stack_name, display_name, description, skills, org_id)
    print(f"  Stack {stack_name!r} saved with {len(skills)} skill(s)")


def install_package_stacks() -> None:
    """Import all curated packages from catalog/packages/ as built-in skill stacks."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("tf_pkg", _PACKAGES_SCRIPT)
    pkg_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pkg_mod)
    packages = pkg_mod.list_packages()
    for p in packages:
        full_pkg = pkg_mod.get_package(p["id"])
        if not full_pkg:
            continue
        skills = [t["name"] for t in full_pkg.get("tools", [])]
        db.save_skill_stack(
            p["id"],
            p["display_name"],
            p.get("tagline", ""),
            skills,
            org_id=None,
            is_builtin=True,
        )
        print(f"  [imported] {p['id']!r}: {len(skills)} tools")


# ---------- self-management routines ----------

def purge_stale(stale_days: int = 90) -> list[str]:
    """Flag skills not used in stale_days as stale in skill_performance."""
    db.init_db()
    conn = db._connect()
    try:
        rows = conn.execute(
            """SELECT skill_name FROM skill_performance
               WHERE julianday('now') - julianday(last_measured) > ?
               AND (error_count + success_count) > 0""",
            (stale_days,),
        ).fetchall()
    finally:
        conn.close()
    stale = [r[0] for r in rows]
    for name in stale:
        print(f"  [stale] {name} — not measured in {stale_days}+ days")
    return stale


def auto_retire_low_performers(error_rate_threshold: float = 0.5,
                                min_calls: int = 10) -> list[str]:
    """Auto-retire skills with error_rate above threshold and sufficient call volume."""
    # error_rate comes from real skill_performance rows, now populated per-invocation
    # by hooks/post-tool-use-perf.py. Before that hook existed the table stayed empty,
    # so this loop saw nothing and never retired — the table, not this accessor, was
    # the gap. Thresholds (0.5 error rate / 10 calls) are unchanged.
    perfs = db.get_skill_performance_all()
    retired = []
    for p in perfs:
        total = p["error_count"] + p["success_count"]
        if total >= min_calls and p["error_rate"] >= error_rate_threshold:
            force_retire(p["skill_name"], reason=f"auto-retire: error_rate {p['error_rate']:.0%}")
            retired.append(p["skill_name"])
    return retired


def rebalance_scores() -> None:
    """Rebuild routing_scores from scratch using current ratings + usage data."""
    db.init_db()
    conn = db._connect()
    try:
        tool_keys = [r[0] for r in conn.execute("SELECT tool_key FROM routing_scores").fetchall()]
    finally:
        conn.close()
    if not tool_keys:
        print("  No routing_scores rows to rebalance.")
        return
    print(f"  Rebalancing {len(tool_keys)} routing score(s)...")
    stats = db.get_rating_stats_bulk([k.split(":", 1)[-1] for k in tool_keys])
    usage = db.get_usage_stats_bulk(tool_keys)
    for key in tool_keys:
        name = key.split(":", 1)[-1]
        stat = stats.get(name, {})
        decayed = stat.get("decayed_avg") or db.BAYES_PRIOR_MEAN
        posterior = (db.BAYES_PRIOR_WEIGHT * db.BAYES_PRIOR_MEAN + stat.get("n", 0) * decayed) / (db.BAYES_PRIOR_WEIGHT + stat.get("n", 0))
        likert_norm = posterior / 5.0
        count = usage.get(key, {}).get("count_30d", 0)
        usage_boost = min(1.0, math.log1p(count) / math.log1p(100))
        composite = likert_norm * 0.6 + usage_boost * 0.4
        db.upsert_routing_score(key, 0.5, 0.5, usage_boost, likert_norm, composite)
    print(f"  Done. {len(tool_keys)} scores updated.")


# ---------- system health ----------

def full_health_report() -> str:
    status_out, had_error = db.status()
    perf = db.get_skill_performance_all()
    pred = db.get_prediction_accuracy()
    eff = db.get_token_efficiency_rank()
    orgs = db.list_org_profiles()
    stacks = db.list_skill_stacks()

    lines = [
        status_out,
        "",
        "======= Performance Health =======",
    ]
    if not perf:
        lines.append("  (no performance data yet)")
    else:
        high_error = [p for p in perf if p["error_rate"] >= 0.3]
        if high_error:
            lines.append("  WARNING — high error-rate skills:")
            for p in high_error:
                lines.append(f"    {p['skill_name']:<30}  error {p['error_rate']:.0%}  ({p['error_count'] + p['success_count']} calls)")
        else:
            lines.append("  All tracked skills healthy (error rate < 30%)")

    lines += [
        "",
        "======= Prediction Stats =======",
        f"  Hit rate: {pred['accuracy']:.1%}" if pred["accuracy"] is not None else "  Hit rate: n/a (no predictions yet)",
        f"  Total predictions: {pred['total']}",
        "",
        "======= Token Efficiency (top 5) =======",
    ]
    if eff:
        for r in eff[:5]:
            lines.append(f"  {r['skill_name']:<30}  avg {r['avg_tokens']:>8.0f} tokens")
    else:
        lines.append("  (no token data yet)")

    lines += [
        "",
        f"======= Organisations ({len(orgs)}) =======",
    ]
    if not orgs:
        lines.append("  (no orgs configured)")
    else:
        for o in orgs:
            lines.append(f"  {o['org_id']:<25}  {o['org_name']}")

    lines += [
        "",
        f"======= Skill Stacks ({len(stacks)}) =======",
    ]
    if not stacks:
        lines.append("  (no stacks — run: toolforge-admin import-packages)")
    else:
        for s in stacks:
            builtin = " [built-in]" if s.get("is_builtin") else ""
            org = f" org:{s['org_id']}" if s.get("org_id") else ""
            lines.append(f"  {s['stack_name']:<30}{builtin}{org}  ({len(s['skills'])} skills)")

    return "\n".join(lines)


# ---------- CLI ----------

def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_admin.py health\n"
        "  toolforge_admin.py retire <tool_name> [<reason>]\n"
        "  toolforge_admin.py override_rating <tool_name> <1.0-5.0>\n"
        "  toolforge_admin.py reset_ratings <tool_name>\n"
        "  toolforge_admin.py create_stack <stack_name> <display_name> <description> <skills_json> [<org_id>]\n"
        "  toolforge_admin.py import-packages\n"
        "  toolforge_admin.py list_stacks [<org_id>]\n"
        "  toolforge_admin.py delete_stack <stack_name>\n"
        "  toolforge_admin.py purge_stale [<days>]\n"
        "  toolforge_admin.py auto_retire [<error_rate_threshold>]\n"
        "  toolforge_admin.py rebalance\n"
        "  toolforge_admin.py list_orgs\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    cmd = argv[1]
    try:
        if cmd == "health":
            print(full_health_report())
            return 0
        if cmd == "retire":
            reason = argv[3] if len(argv) > 3 else "admin-retired"
            force_retire(argv[2], reason)
            return 0
        if cmd == "override_rating":
            override_rating(argv[2], float(argv[3]))
            return 0
        if cmd == "reset_ratings":
            reset_ratings(argv[2])
            return 0
        if cmd == "create_stack":
            org = argv[6] if len(argv) > 6 else None
            skills = json.loads(argv[5])
            create_stack(argv[2], argv[3], argv[4], skills, org)
            return 0
        if cmd == "import-packages":
            install_package_stacks()
            print("  Done. Run list_stacks to verify.")
            return 0
        if cmd == "list_stacks":
            org = argv[2] if len(argv) > 2 else None
            stacks = db.list_skill_stacks(org)
            print(json.dumps(stacks, indent=2))
            return 0
        if cmd == "delete_stack":
            ok = db.delete_skill_stack(argv[2])
            print("ok" if ok else "not found (or built-in)")
            return 0 if ok else 1
        if cmd == "purge_stale":
            days = int(argv[2]) if len(argv) > 2 else 90
            stale = purge_stale(days)
            print(f"  {len(stale)} stale skill(s) flagged")
            return 0
        if cmd == "auto_retire":
            threshold = float(argv[2]) if len(argv) > 2 else 0.5
            retired = auto_retire_low_performers(threshold)
            print(f"  {len(retired)} skill(s) auto-retired")
            return 0
        if cmd == "rebalance":
            rebalance_scores()
            return 0
        if cmd == "list_orgs":
            print(json.dumps(db.list_org_profiles(), indent=2))
            return 0
    except (IndexError, ValueError) as exc:
        print(f"toolforge_admin: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"toolforge_admin: {exc}", file=sys.stderr)
        return 3
    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
