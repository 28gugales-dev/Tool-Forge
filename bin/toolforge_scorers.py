#!/usr/bin/env python3
"""ToolForge pluggable scorer registry. Turns raw skill_performance / token_stats
rows into objective quality scores in [0, 1] (higher is better).

Design (ported from EvoSkill's make_scorer dispatch idea): scorers live in a
module-level SCORERS dict mapping name -> callable(skill_name, db_rows) -> float.
A scorer is a *pure function* of the rows handed to it — it never touches the DB
itself, so it stays trivially testable and the CLI owns all I/O. New scorers plug
in via register(name, fn) without editing the dispatch core. score_with() is the
single dispatch entry point (the EvoSkill make_scorer analogue): it looks the
scorer up by name and rejects unknown names with KeyError.

db_rows contract (a plain dict, all keys optional so scorers degrade gracefully):
    {
      "perf":  {skill_name, avg_latency_ms, error_count, success_count, token_avg}
               or None when the skill has no skill_performance row,
      "token": {sessions, avg_tokens, min_tokens, max_tokens}
               or None when the skill has no token_stats data,
    }

Token efficiency reads token_stats (via the "token" row) when present and falls
back to skill_performance.token_avg otherwise — see _token_efficiency.

CLI exit codes: 0 success, 1 no data, 2 usage error, 3 I/O error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

# Allow running from bin/ or repo root.
sys.path.insert(0, str(Path(__file__).parent))
import toolforge_db as db


# ---------- section: tunables ----------

# Latency budget: a skill at/under this median latency scores 1.0 on latency,
# decaying linearly to 0.0 at 2x budget. 2s matches the perf hook's soft target.
LATENCY_BUDGET_MS = 2000.0

# Token budget for the fallback token-efficiency curve when no token_stats exist:
# token_avg at/under this scores ~1.0, decaying toward 0 as usage balloons.
TOKEN_BUDGET = 1500.0

# Composite weights over the three signals. Renormalized at runtime across only
# the signals that produced a value, so a missing signal never silently counts 0.
_COMPOSITE_WEIGHTS = {
    "reliability": 0.4,
    "latency": 0.3,
    "token_efficiency": 0.3,
}


# ---------- section: helpers ----------

def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _total_calls(perf: Optional[dict]) -> int:
    if not perf:
        return 0
    return int(perf.get("error_count", 0)) + int(perf.get("success_count", 0))


# ---------- section: built-in scorers ----------
# Each scorer returns a float in [0, 1] (higher = better) or raises NoData when
# the rows carry no usable signal, so the dispatcher can report "no data" cleanly.


class NoData(Exception):
    """Raised by a scorer when db_rows carries no signal it can score."""


def _latency(skill_name: str, rows: dict) -> float:
    """Median (here: EMA avg) latency vs the budget. Lower latency scores higher."""
    perf = rows.get("perf")
    if not perf or _total_calls(perf) == 0:
        raise NoData("no latency data")
    lat = float(perf.get("avg_latency_ms", 0.0))
    # 1.0 at/under budget, linear decay to 0.0 at 2x budget.
    return _clamp01(1.0 - max(0.0, lat - LATENCY_BUDGET_MS) / LATENCY_BUDGET_MS)


def _reliability(skill_name: str, rows: dict) -> float:
    """1 - error rate. A skill with no calls has no reliability signal."""
    perf = rows.get("perf")
    total = _total_calls(perf)
    if total == 0:
        raise NoData("no reliability data")
    errors = int(perf.get("error_count", 0))
    return _clamp01(1.0 - errors / total)


def _token_efficiency(skill_name: str, rows: dict) -> float:
    """Fewer tokens per use scores higher. Prefers token_stats.avg_tokens; falls
    back to skill_performance.token_avg when no token_stats row exists."""
    token = rows.get("token")
    avg = None
    if token and token.get("avg_tokens") is not None:
        avg = float(token["avg_tokens"])
    else:
        perf = rows.get("perf")
        if perf and float(perf.get("token_avg", 0.0)) > 0.0:
            avg = float(perf["token_avg"])
    if avg is None or avg <= 0.0:
        raise NoData("no token data")
    # 1.0 at/under budget, linear decay to 0.0 at 2x budget.
    return _clamp01(1.0 - max(0.0, avg - TOKEN_BUDGET) / TOKEN_BUDGET)


def _composite(skill_name: str, rows: dict) -> float:
    """Weighted blend of reliability/latency/token_efficiency, renormalized over
    only the signals that are actually present. If none are present, NoData."""
    parts: list[tuple[float, float]] = []  # (weight, score)
    for name, weight in _COMPOSITE_WEIGHTS.items():
        try:
            parts.append((weight, SCORERS[name](skill_name, rows)))
        except NoData:
            continue
    if not parts:
        raise NoData("no signals for composite")
    wsum = sum(w for w, _ in parts)
    return _clamp01(sum(w * s for w, s in parts) / wsum)


# ---------- section: registry ----------

SCORERS: dict[str, Callable[[str, dict], float]] = {
    "latency": _latency,
    "reliability": _reliability,
    "token_efficiency": _token_efficiency,
    "composite": _composite,
}


def register(name: str, fn: Callable[[str, dict], float]) -> None:
    """Plug a new scorer into the registry. Future scorers call this at import
    time and never touch the dispatch core — the EvoSkill make_scorer pattern."""
    key = name.strip()
    if not key:
        raise ValueError("scorer name must be non-empty")
    if not callable(fn):
        raise TypeError("scorer fn must be callable")
    SCORERS[key] = fn


def score_with(scorer: str, skill_name: str, rows: dict) -> float:
    """Single dispatch entry point. Raises KeyError for an unknown scorer."""
    if scorer not in SCORERS:
        raise KeyError(scorer)
    return SCORERS[scorer](skill_name, rows)


# ---------- section: data loading ----------

def _load_rows(skill_name: str) -> dict:
    """Fetch the raw rows a scorer needs, via existing DB helpers only."""
    perf = db.get_skill_performance(skill_name)
    token = db.get_token_stats_bulk([skill_name]).get(skill_name)
    return {"perf": perf, "token": token}


def _scored_skill_names() -> list[str]:
    """All skill names that have a skill_performance row (the scoreable universe)."""
    return [p["skill_name"] for p in db.get_skill_performance_all()]


# ---------- section: scoring operations ----------

def score_skill(skill_name: str, scorer: str = "composite") -> Optional[float]:
    """Score one skill. Returns the score (also persisted), or None on no data."""
    rows = _load_rows(skill_name)
    try:
        value = score_with(scorer, skill_name, rows)
    except NoData:
        return None
    db.record_scorer_result(skill_name, scorer, value, _detail_for(rows))
    return value


def leaderboard(scorer: str = "composite") -> list[dict]:
    """Rank every scoreable skill by the given scorer, best first. Skills with no
    signal for that scorer are dropped. Results are persisted per skill."""
    ranked: list[dict] = []
    for name in _scored_skill_names():
        rows = _load_rows(name)
        try:
            value = score_with(scorer, name, rows)
        except NoData:
            continue
        db.record_scorer_result(name, scorer, value, _detail_for(rows))
        ranked.append({"skill_name": name, "scorer": scorer, "score": round(value, 4)})
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked


def _detail_for(rows: dict) -> str:
    """Compact provenance string stored alongside each scorer_result row."""
    perf = rows.get("perf")
    token = rows.get("token")
    calls = _total_calls(perf)
    avg_tok = (token or {}).get("avg_tokens")
    if avg_tok is None and perf:
        avg_tok = perf.get("token_avg")
    return f"calls={calls} avg_tok={round(avg_tok, 1) if avg_tok else 0}"


# ---------- section: CLI ----------

def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_scorers.py score <skill> [--scorer NAME] [--json]\n"
        "  toolforge_scorers.py leaderboard [--scorer NAME] [--json]\n"
        "  toolforge_scorers.py list-scorers\n"
        "  toolforge_scorers.py --self-test\n"
    )


def _parse_opts(args: list[str]) -> tuple[list[str], str, bool]:
    """Manual argv parse (no argparse, matching repo convention). Returns
    (positionals, scorer, as_json). Raises ValueError on a malformed flag."""
    positionals: list[str] = []
    scorer = "composite"
    as_json = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--scorer":
            if i + 1 >= len(args):
                raise ValueError("--scorer needs a value")
            scorer = args[i + 1]
            i += 2
            continue
        if a == "--json":
            as_json = True
            i += 1
            continue
        if a.startswith("--"):
            raise ValueError(f"unknown flag {a!r}")
        positionals.append(a)
        i += 1
    return positionals, scorer, as_json


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2

    cmd = argv[1]

    if cmd == "--self-test":
        return _self_test()

    if cmd == "list-scorers":
        names = sorted(SCORERS)
        if "--json" in argv[2:]:
            print(json.dumps(names))
        else:
            print("Registered scorers:")
            for n in names:
                print(f"  {n}")
        return 0

    try:
        positionals, scorer, as_json = _parse_opts(argv[2:])
    except ValueError as exc:
        print(f"toolforge_scorers: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2

    if scorer not in SCORERS:
        print(
            f"toolforge_scorers: unknown scorer {scorer!r}. "
            f"Known: {', '.join(sorted(SCORERS))}",
            file=sys.stderr,
        )
        return 2

    try:
        if cmd == "score":
            if not positionals:
                print("toolforge_scorers: score needs a <skill>", file=sys.stderr)
                return 2
            skill = positionals[0]
            value = score_skill(skill, scorer)
            if value is None:
                print(f"no data for {skill!r} under scorer {scorer!r}", file=sys.stderr)
                return 1
            if as_json:
                print(json.dumps({"skill_name": skill, "scorer": scorer, "score": round(value, 4)}))
            else:
                print(f"{skill}  {scorer}  {value:.4f}")
            return 0

        if cmd == "leaderboard":
            ranked = leaderboard(scorer)
            if not ranked:
                print(f"no data to rank under scorer {scorer!r}", file=sys.stderr)
                return 1
            if as_json:
                print(json.dumps(ranked))
            else:
                print(f"======= Leaderboard ({scorer}) =======")
                for i, r in enumerate(ranked, 1):
                    print(f"  {i:>2}. {r['skill_name']:<30} {r['score']:.4f}")
            return 0
    except (db.sqlite3.Error, OSError) as exc:
        print(f"toolforge_scorers: db error: {exc}", file=sys.stderr)
        return 3

    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


# ---------- section: self-test ----------

def _self_test() -> int:
    """Exercise every scorer, composite renormalization with missing signals,
    unknown-scorer rejection, and the empty-data path — all against a temp DB.

    Does NOT touch ~/.claude/toolforge.db (DB_PATH temp-swap, mirroring
    toolforge_db._self_test).
    """
    import tempfile

    saved = db.DB_PATH
    tmpdir = Path(tempfile.mkdtemp(prefix="toolforge_scorers_test_"))
    db.DB_PATH = tmpdir / "toolforge.db"
    passed = 0
    failed = 0

    def check(label: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        if cond:
            print(f"OK: {label}")
            passed += 1
        else:
            print(f"FAIL: {label} {extra}")
            failed += 1

    try:
        db.init_db()

        # Seed synthetic skill_performance: 'fast' (clean+quick), 'flaky' (errors),
        # 'slow' (over budget), plus token_stats for one of them.
        # upsert_skill_performance(name, latency_ms, success, tokens)
        for _ in range(8):
            db.upsert_skill_performance("fast", 200.0, True, 400)
        db.upsert_skill_performance("flaky", 300.0, True, 500)
        for _ in range(4):
            db.upsert_skill_performance("flaky", 300.0, False, 500)
        for _ in range(5):
            db.upsert_skill_performance("slow", 5000.0, True, 3000)
        # token_stats rows for 'fast' so token_efficiency prefers them.
        for _ in range(3):
            db.log_token_stats("sess-1", "fast", 100, 100)  # total 200/use

        fast_rows = _load_rows("fast")
        flaky_rows = _load_rows("flaky")
        slow_rows = _load_rows("slow")

        # 1. reliability: fast (all success) == 1.0, flaky (4/5 err) == 0.2
        rel_fast = score_with("reliability", "fast", fast_rows)
        rel_flaky = score_with("reliability", "flaky", flaky_rows)
        check("reliability scores error rate",
              abs(rel_fast - 1.0) < 1e-9 and abs(rel_flaky - 0.2) < 1e-9,
              f"(fast={rel_fast}, flaky={rel_flaky})")

        # 2. latency: fast under budget -> 1.0; slow at >2x budget -> 0.0
        lat_fast = score_with("latency", "fast", fast_rows)
        lat_slow = score_with("latency", "slow", slow_rows)
        check("latency rewards fast / penalizes slow",
              abs(lat_fast - 1.0) < 1e-9 and lat_slow == 0.0,
              f"(fast={lat_fast}, slow={lat_slow})")

        # 3. token_efficiency prefers token_stats (fast=200/use -> ~1.0)
        tok_fast = score_with("token_efficiency", "fast", fast_rows)
        check("token_efficiency reads token_stats",
              abs(tok_fast - 1.0) < 1e-9, f"(fast={tok_fast})")

        # 4. token_efficiency falls back to skill_performance.token_avg when no
        # token_stats row exists (slow has token_avg ~3000 -> 0.0 at 2x budget)
        tok_slow = score_with("token_efficiency", "slow", slow_rows)
        check("token_efficiency falls back to token_avg",
              slow_rows["token"] is None and tok_slow == 0.0,
              f"(slow={tok_slow}, token_row={slow_rows['token']})")

        # 5. composite blends all three signals for a fully-populated skill
        comp_fast = score_with("composite", "fast", fast_rows)
        expected_fast = 0.4 * rel_fast + 0.3 * lat_fast + 0.3 * tok_fast
        check("composite blends all signals",
              abs(comp_fast - expected_fast) < 1e-9,
              f"(got={comp_fast}, expected={expected_fast})")

        # 6. composite renormalizes when a signal is missing. Build rows with perf
        # but force the token signal absent so only reliability+latency remain;
        # weights renormalize over 0.4+0.3=0.7.
        partial_rows = {"perf": dict(flaky_rows["perf"]), "token": None}
        partial_rows["perf"]["token_avg"] = 0.0  # kill the token fallback too
        comp_partial = score_with("composite", "flaky", partial_rows)
        rel_p = score_with("reliability", "flaky", partial_rows)
        lat_p = score_with("latency", "flaky", partial_rows)
        expected_partial = (0.4 * rel_p + 0.3 * lat_p) / 0.7
        check("composite renormalizes over present signals",
              abs(comp_partial - expected_partial) < 1e-9,
              f"(got={comp_partial}, expected={expected_partial})")

        # 7. unknown scorer rejected
        try:
            score_with("does-not-exist", "fast", fast_rows)
            check("unknown scorer rejected", False, "(no KeyError raised)")
        except KeyError:
            check("unknown scorer rejected", True)

        # 8. empty data -> NoData on every built-in scorer (skill with no rows)
        empty_rows = _load_rows("ghost-skill")
        empties = 0
        for name in ("latency", "reliability", "token_efficiency", "composite"):
            try:
                score_with(name, "ghost-skill", empty_rows)
            except NoData:
                empties += 1
        check("empty data -> NoData for all scorers", empties == 4,
              f"({empties}/4)")

        # 9. score_skill returns None on no data (graceful), value on data
        none_result = score_skill("ghost-skill", "composite")
        val_result = score_skill("fast", "composite")
        check("score_skill: None on no data, value on data",
              none_result is None and val_result is not None
              and abs(val_result - comp_fast) < 1e-9,
              f"(none={none_result}, val={val_result})")

        # 10. results persisted via record_scorer_result
        persisted = db.get_scorer_results("fast", scorer="composite")
        check("scorer result persisted to DB",
              len(persisted) >= 1 and persisted[0]["scorer"] == "composite",
              f"(rows={len(persisted)})")

        # 11. leaderboard ranks scoreable skills best-first, drops no-signal ones
        board = leaderboard("composite")
        names_in_board = [r["skill_name"] for r in board]
        sorted_desc = all(
            board[i]["score"] >= board[i + 1]["score"] for i in range(len(board) - 1)
        )
        check("leaderboard ranks best-first over real skills",
              "fast" in names_in_board and sorted_desc and len(board) == 3,
              f"(board={board})")

        # 12. register() plugs a new scorer in without touching the core
        register("always-half", lambda s, r: 0.5)
        half = score_with("always-half", "fast", fast_rows)
        check("register() adds a custom scorer",
              "always-half" in SCORERS and abs(half - 0.5) < 1e-9)
        # keep the registry clean for any later in-process callers
        SCORERS.pop("always-half", None)

    finally:
        db.DB_PATH = saved
        try:
            for p in tmpdir.iterdir():
                p.unlink()
            tmpdir.rmdir()
        except OSError:
            pass

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
