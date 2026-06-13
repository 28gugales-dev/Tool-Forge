#!/usr/bin/env python3
"""ToolForge predictive engine. Analyses historical install, usage, and
pipeline data to predict which skills a new session is likely to need.

Prediction is purely SQL-based — no ML model, no external calls.

Algorithm:
  1. Count how often each skill was used in the last N sessions (recency-weighted).
  2. Find pipeline chains that share the same starting skill as the current prompt.
  3. Cross-reference with the router's TF-IDF suggestions for the current prompt.
  4. Output a ranked list with confidence scores.

CLI exit codes: 0 success, 1 no predictions, 2 usage error, 3 I/O error.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import toolforge_db as db

_DB_SCRIPT = Path(__file__).parent / "toolforge_db.py"
_ROUTER_SCRIPT = Path(__file__).parent / "toolforge_router.py"


# ---------- signal sources ----------

def _recency_weighted_skills(limit_sessions: int = 20) -> dict[str, float]:
    """Skills used recently score higher. Decay: each session back loses 10%."""
    try:
        recent = db.get_recent_pipelines(limit_sessions)
    except sqlite3.Error as exc:
        print(f"toolforge_predictor: recency signal unavailable: {exc}", file=sys.stderr)
        return {}
    scores: dict[str, float] = {}
    for i, pipeline in enumerate(recent):
        decay = math.exp(-0.1 * i)
        try:
            steps = json.loads(pipeline.get("steps_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
        for step in steps:
            skill = step.get("skill_name") or step.get("skill")
            if skill and skill != "(built-in)":
                scores[skill] = scores.get(skill, 0.0) + decay
    # Normalise to [0, 1]
    if scores:
        max_val = max(scores.values())
        if max_val > 0:
            scores = {k: round(v / max_val, 3) for k, v in scores.items()}
    return scores


def _usage_frequency_skills(top_n: int = 15) -> dict[str, float]:
    """Skills with high 30-day usage counts get a frequency signal."""
    # Pull usage from usage_stats table via db module directly.
    db.init_db()
    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT tool_key, count_30d FROM usage_stats ORDER BY count_30d DESC LIMIT ?",
            (top_n,),
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"toolforge_predictor: frequency signal unavailable: {exc}", file=sys.stderr)
        return {}
    finally:
        conn.close()
    if not rows:
        return {}
    max_count = rows[0][1] if rows[0][1] > 0 else 1
    return {row[0].split(":", 1)[-1]: round(row[1] / max_count, 3) for row in rows}


def _pipeline_chain_skills(current_skill: str | None) -> dict[str, float]:
    """If current_skill is known, find which skills typically follow it in pipelines."""
    if not current_skill:
        return {}
    try:
        recent = db.get_recent_pipelines(50)
    except sqlite3.Error as exc:
        print(f"toolforge_predictor: chain signal unavailable: {exc}", file=sys.stderr)
        return {}
    successors: dict[str, int] = {}
    for pipeline in recent:
        try:
            steps = json.loads(pipeline.get("steps_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
        skill_names = [s.get("skill_name") or s.get("skill") for s in steps]
        for i, name in enumerate(skill_names):
            if name == current_skill and i + 1 < len(skill_names):
                nxt = skill_names[i + 1]
                if nxt and nxt != "(built-in)":
                    successors[nxt] = successors.get(nxt, 0) + 1
    if not successors:
        return {}
    max_cnt = max(successors.values())
    return {k: round(v / max_cnt, 3) for k, v in successors.items()}


def _router_suggestions(prompt: str) -> dict[str, float]:
    """Run the TF-IDF router on the prompt and return {skill: score} mapping.

    Router CLI contract: `route <prompt...> --json` prints a JSON array of
    {name, score, description, installed}. Scores are raw cosine (small, ~0..0.4)
    so we max-normalise to [0, 1] to match the other signals before merge.
    """
    if not prompt or not _ROUTER_SCRIPT.exists():
        return {}
    try:
        result = subprocess.run(
            [sys.executable, str(_ROUTER_SCRIPT), "route", prompt, "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            print(
                f"toolforge_predictor: router subcall exited {result.returncode}: "
                f"{result.stderr.strip()[:200]}",
                file=sys.stderr,
            )
            return {}
        if not result.stdout.strip():
            return {}
        data = json.loads(result.stdout)
        scores: dict[str, float] = {}
        if isinstance(data, list):
            for item in data:
                name = item.get("name") or item.get("skill_name")
                if name:
                    scores[name] = float(item.get("score", 0.0))
        elif isinstance(data, dict):
            scores = {k: float(v) for k, v in data.items()}
        if scores:
            max_val = max(scores.values())
            if max_val > 0:
                scores = {k: round(v / max_val, 3) for k, v in scores.items()}
        return scores
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"toolforge_predictor: router subcall failed: {exc}", file=sys.stderr)
        return {}


# ---------- merge & rank ----------

def predict(prompt: str = "", current_skill: str | None = None,
            top_n: int = 5) -> list[dict]:
    """Return top_n predicted skills with confidence scores."""
    recency = _recency_weighted_skills()
    frequency = _usage_frequency_skills()
    chain = _pipeline_chain_skills(current_skill)
    router = _router_suggestions(prompt) if prompt else {}

    # Weight the four signals: router has highest signal when a prompt is given
    weights = {
        "router": 0.40 if prompt else 0.0,
        "chain": 0.30 if current_skill else 0.0,
        "recency": 0.20,
        "frequency": 0.10,
    }
    # Redistribute weights if some signals are absent
    total_w = sum(weights.values())
    if total_w < 0.99 and total_w > 0:
        factor = 1.0 / total_w
        weights = {k: v * factor for k, v in weights.items()}

    all_skills = set(recency) | set(frequency) | set(chain) | set(router)
    combined: dict[str, float] = {}
    for skill in all_skills:
        score = (
            weights["router"] * router.get(skill, 0.0)
            + weights["chain"] * chain.get(skill, 0.0)
            + weights["recency"] * recency.get(skill, 0.0)
            + weights["frequency"] * frequency.get(skill, 0.0)
        )
        if score > 0.01:
            combined[skill] = round(score, 3)

    ranked = sorted(combined.items(), key=lambda x: -x[1])[:top_n]
    return [{"skill": skill, "confidence": conf} for skill, conf in ranked]


def predict_and_log(session_id: str, prompt: str = "",
                    current_skill: str | None = None, top_n: int = 5) -> list[dict]:
    """Predict and persist predictions for later accuracy measurement."""
    predictions = predict(prompt, current_skill, top_n)
    for p in predictions:
        try:
            db.log_prediction(session_id, p["skill"], p["confidence"])
        except Exception:
            pass
    return predictions


def render_predictions(predictions: list[dict]) -> str:
    if not predictions:
        return "[ToolForge predictor] No strong predictions for this session."
    lines = ["[ToolForge predictor] Skills likely needed this session:"]
    for i, p in enumerate(predictions, 1):
        # ASCII bar — '#' encodes under any console codepage (Windows cp1252
        # can't encode the U+2588 block char and would raise UnicodeEncodeError).
        bar = "#" * int(p["confidence"] * 10)
        lines.append(f"  {i}. {p['skill']:<30} {bar:<10} ({p['confidence']:.0%})")
    return "\n".join(lines)


# ---------- accuracy reporting ----------

def accuracy_report() -> str:
    stats = db.get_prediction_accuracy()
    top = db.get_top_predicted_skills(5)
    lines = [
        "======= Prediction Accuracy Report =======",
        f"  Total predictions:  {stats['total']}",
        f"  Confirmed hits:     {stats['hits']}",
        f"  Hit rate:           {stats['accuracy']:.1%}" if stats["accuracy"] is not None else "  Hit rate:           n/a",
        f"  Avg confidence:     {stats['avg_confidence']:.1%}" if stats["avg_confidence"] is not None else "  Avg confidence:     n/a",
        "",
        "  Top predicted skills:",
    ]
    for s in top:
        lines.append(
            f"    {s['skill']:<30}  hits {s['hits']:>3}/{s['predictions']:<3}  "
            f"hit-rate {s['hit_rate']:.0%}  avg-conf {s['avg_confidence']:.0%}"
        )
    lines.append("=" * 43)
    return "\n".join(lines)


# ---------- self-test ----------

def _self_test() -> int:
    import tempfile

    passed = failed = 0

    # ---- pure-function checks (no DB) ----

    # render_predictions: empty -> "No strong predictions" line
    if "No strong predictions" in render_predictions([]):
        print("OK: render_predictions handles empty list")
        passed += 1
    else:
        print("FAIL: render_predictions empty list")
        failed += 1

    rendered = render_predictions([{"skill": "gsap-core", "confidence": 0.8}])
    if "gsap-core" in rendered and "80%" in rendered:
        print("OK: render_predictions shows skill name and confidence")
        passed += 1
    else:
        print(f"FAIL: render_predictions content:\n{rendered}")
        failed += 1

    # Output must encode under cp1252 — the CLI prints to a Windows console that
    # can't represent block-drawing glyphs (regression guard for the bar char).
    try:
        rendered.encode("cp1252")
        print("OK: render_predictions output is cp1252-safe")
        passed += 1
    except UnicodeEncodeError as exc:
        print(f"FAIL: render_predictions not cp1252-safe: {exc}")
        failed += 1

    # ---- router subcall: prove the fixed call path returns real JSON ----
    router_scores = _router_suggestions("animate React component with GSAP scroll triggers")
    if isinstance(router_scores, dict):
        # Values are max-normalised to [0, 1] when present.
        ok_range = all(0.0 <= v <= 1.0 for v in router_scores.values())
        print(f"OK: router subcall returned dict ({len(router_scores)} skills), normalised={ok_range}")
        passed += 1 if ok_range else 0
        failed += 0 if ok_range else 1
        if not ok_range:
            print(f"FAIL: router scores outside [0,1]: {router_scores}")
    else:
        print(f"FAIL: router subcall returned {type(router_scores)}")
        failed += 1

    # ---- DB-backed checks (temp DB via toolforge_db global swap) ----
    original_db = db.DB_PATH
    tmp = Path(tempfile.mkdtemp()) / "predictor_test.db"
    db.DB_PATH = tmp
    try:
        db.init_db()

        # Seed pipelines so recency + chain signals have data.
        steps = [
            {"skill_name": "gsap-core", "skill_type": "skill"},
            {"skill_name": "code-review", "skill_type": "skill"},
        ]
        db.save_pipeline("animate then review", "h1", json.dumps(steps), True)
        db.upsert_usage_stats("skill:gsap-core", 12, "2026-01-01T00:00:00Z")
        db.upsert_usage_stats("skill:code-review", 3, "2026-01-01T00:00:00Z")

        recency = _recency_weighted_skills()
        if "gsap-core" in recency and "code-review" in recency:
            print("OK: recency signal picks up seeded pipeline skills")
            passed += 1
        else:
            print(f"FAIL: recency signal = {recency}")
            failed += 1

        freq = _usage_frequency_skills()
        if freq.get("gsap-core", 0) >= freq.get("code-review", 0) > 0:
            print("OK: frequency signal ranks by usage count")
            passed += 1
        else:
            print(f"FAIL: frequency signal = {freq}")
            failed += 1

        chain = _pipeline_chain_skills("gsap-core")
        if chain.get("code-review", 0) > 0:
            print("OK: chain signal finds successor skill")
            passed += 1
        else:
            print(f"FAIL: chain signal = {chain}")
            failed += 1

        preds = predict(prompt="", current_skill="gsap-core", top_n=5)
        if isinstance(preds, list) and preds and all("skill" in p and "confidence" in p for p in preds):
            print(f"OK: predict() returns ranked list ({len(preds)} skills)")
            passed += 1
        else:
            print(f"FAIL: predict() = {preds}")
            failed += 1

        logged = predict_and_log("sess-test", prompt="", current_skill="gsap-core")
        acc = db.get_prediction_accuracy()
        if logged and acc["total"] >= len(logged):
            print(f"OK: predict_and_log persisted {len(logged)} prediction(s) (total={acc['total']})")
            passed += 1
        else:
            print(f"FAIL: predict_and_log logged={logged} acc={acc}")
            failed += 1

        if logged:
            db.confirm_prediction("sess-test", logged[0]["skill"])
            acc2 = db.get_prediction_accuracy()
            if acc2["hits"] >= 1:
                print("OK: confirm_prediction recorded a hit")
                passed += 1
            else:
                print(f"FAIL: confirm_prediction acc={acc2}")
                failed += 1
    finally:
        db.DB_PATH = original_db
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- CLI ----------

def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_predictor.py predict [<prompt>]\n"
        "  toolforge_predictor.py predict_and_log <session_id> [<prompt>]\n"
        "  toolforge_predictor.py confirm <session_id> <skill_name>\n"
        "  toolforge_predictor.py accuracy\n"
        "  toolforge_predictor.py top_predicted [<limit>]\n"
        "  toolforge_predictor.py --self-test\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "--self-test":
        return _self_test()
    try:
        if cmd == "predict":
            prompt = " ".join(argv[2:]) if len(argv) > 2 else ""
            preds = predict(prompt)
            if not preds:
                print("[]")
                return 1
            print(json.dumps(preds, indent=2))
            return 0
        if cmd == "predict_and_log":
            session_id = argv[2]
            prompt = " ".join(argv[3:]) if len(argv) > 3 else ""
            preds = predict_and_log(session_id, prompt)
            print(render_predictions(preds))
            print(json.dumps(preds))
            return 0
        if cmd == "confirm":
            db.confirm_prediction(argv[2], argv[3])
            print("ok")
            return 0
        if cmd == "accuracy":
            print(accuracy_report())
            return 0
        if cmd == "top_predicted":
            limit = int(argv[2]) if len(argv) > 2 else 10
            print(json.dumps(db.get_top_predicted_skills(limit)))
            return 0
    except (IndexError, ValueError) as exc:
        print(f"toolforge_predictor: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"toolforge_predictor: {exc}", file=sys.stderr)
        return 3
    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
