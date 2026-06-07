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
    recent = db.get_recent_pipelines(limit_sessions)
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
    recent = db.get_recent_pipelines(50)
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
    """Run the TF-IDF router on the prompt and return {skill: score} mapping."""
    if not prompt or not _ROUTER_SCRIPT.exists():
        return {}
    try:
        result = subprocess.run(
            [sys.executable, str(_ROUTER_SCRIPT), "--json", prompt],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            # Router returns list of {skill_name, score} or similar
            if isinstance(data, list):
                return {item.get("skill_name", item.get("name", "")): item.get("score", 0.0)
                        for item in data if item.get("skill_name") or item.get("name")}
            if isinstance(data, dict):
                return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
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
        bar = "█" * int(p["confidence"] * 10)
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


# ---------- CLI ----------

def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_predictor.py predict [<prompt>]\n"
        "  toolforge_predictor.py predict_and_log <session_id> [<prompt>]\n"
        "  toolforge_predictor.py confirm <session_id> <skill_name>\n"
        "  toolforge_predictor.py accuracy\n"
        "  toolforge_predictor.py top_predicted [<limit>]\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    cmd = argv[1]
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
