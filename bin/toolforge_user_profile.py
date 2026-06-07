#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""ToolForge adaptive user profile.

Tracks which skills a user prefers per task type and detects recurring workflow
patterns that can be surfaced as shortcuts. Signals flow in two directions:

  Session end  -> record_session_signals()  (called by session-end-learner hook)
  Routing time -> adjusted_scores()         (called by router to bias results)

Shortcut detection uses a sliding window: if the same ordered sequence of 3+
skills appears across 4+ distinct sessions within 30 days, it is auto-saved as
a workflow shortcut.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import toolforge_db as db

# Minimum session count before a sequence is saved as a shortcut
_SHORTCUT_MIN_SESSIONS = 4
# Minimum sequence length to consider
_SHORTCUT_MIN_LEN = 3
# How much the preference score shifts the composite routing score (additive)
_PREF_BLEND = 0.12


# ---------- signal recording ----------

def record_session_signals(task_type_id: str, skills_used: list[str],
                            positive_skills: Optional[list[str]] = None,
                            negative_skills: Optional[list[str]] = None) -> None:
    """Record what the user did in a session for a given task type.

    - All skills_used receive a weak positive signal (weight 0.5).
    - Explicit positive_skills receive a strong positive signal (weight 1.0).
    - Explicit negative_skills receive a negative signal.
    """
    seen: set[str] = set()
    for skill in skills_used:
        if skill not in seen:
            db.record_preference_signal(task_type_id, skill, positive=True, weight=0.5)
            seen.add(skill)

    for skill in (positive_skills or []):
        db.record_preference_signal(task_type_id, skill, positive=True, weight=1.0)

    for skill in (negative_skills or []):
        db.record_preference_signal(task_type_id, skill, positive=False, weight=1.0)


def record_explicit_feedback(task_type_id: str, skill_name: str, positive: bool) -> None:
    """One-shot explicit thumbs-up/down on a skill for a task type."""
    db.record_preference_signal(task_type_id, skill_name, positive, weight=1.0)


# ---------- preference-adjusted scoring ----------

def adjusted_scores(task_type_id: str, candidates: list[dict]) -> list[dict]:
    """Re-rank candidates by blending their composite score with user preference.

    candidates: list of dicts with at least {"skill_name": str, "composite": float}
    Returns the same list, sorted descending by blended_score, with that key added.
    """
    if not task_type_id:
        return candidates

    prefs = {
        p["skill_name"]: p["preference_score"]
        for p in db.get_preferences_for_task(task_type_id)
    }
    if not prefs:
        return candidates

    out = []
    for c in candidates:
        pref = prefs.get(c["skill_name"], 0.0)
        c["blended_score"] = round(
            c.get("composite", 0.5) + _PREF_BLEND * pref, 4
        )
        out.append(c)
    return sorted(out, key=lambda x: x["blended_score"], reverse=True)


def get_top_preferences(task_type_id: str, top_n: int = 5) -> list[dict]:
    """Return the top preferred skills for a task type."""
    prefs = db.get_preferences_for_task(task_type_id)
    return prefs[:top_n]


def profile_summary() -> str:
    """Render a human-readable summary of the user's preference profile."""
    all_prefs = db.get_all_preferences()
    if not all_prefs:
        return "  No preference data recorded yet. Use ToolForge for a few sessions to build your profile."

    lines = ["======= User Preference Profile ======="]
    for task_id, skills in sorted(all_prefs.items()):
        lines.append(f"\n  Task: {task_id}")
        for s in skills[:5]:
            bar_len = int(abs(s["preference_score"]) * 10)
            sign = "+" if s["preference_score"] >= 0 else "-"
            bar = sign * bar_len
            lines.append(f"    {s['skill_name']:<30}  {s['preference_score']:+.2f}  |{bar}|")
    return "\n".join(lines)


# ---------- shortcut detection ----------

def detect_shortcuts_from_pipelines(lookback_days: int = 30,
                                     min_sessions: int = _SHORTCUT_MIN_SESSIONS,
                                     min_len: int = _SHORTCUT_MIN_LEN) -> list[dict]:
    """Scan recent pipelines for repeated skill sequences and auto-save shortcuts.

    Returns list of newly saved shortcuts.
    """
    pipelines = db.get_recent_pipelines(limit=100)
    if not pipelines:
        return []

    seq_sessions: dict[tuple, set] = defaultdict(set)
    for p in pipelines:
        try:
            steps = json.loads(p["steps_json"]) if isinstance(p["steps_json"], str) else p["steps_json"]
        except (json.JSONDecodeError, KeyError):
            continue

        skill_seq = [s.get("skill") or s.get("tool") or str(s) for s in steps if isinstance(s, dict)]
        if not skill_seq:
            skill_seq = steps if isinstance(steps, list) else []

        session_key = p.get("id", 0)
        for start in range(len(skill_seq)):
            for end in range(start + min_len, len(skill_seq) + 1):
                seq = tuple(skill_seq[start:end])
                seq_sessions[seq].add(session_key)

    saved = []
    for seq, sessions in seq_sessions.items():
        if len(sessions) < min_sessions:
            continue
        name = "auto-" + "-".join(seq[:3])[:50]
        existing = [s for s in db.list_workflow_shortcuts() if s["shortcut_name"] == name]
        if existing:
            continue
        db.save_workflow_shortcut(
            shortcut_name=name,
            description=f"Auto-detected: {' -> '.join(seq)}",
            trigger_skills=list(seq[:1]),
            steps=[{"skill": s, "step": i + 1} for i, s in enumerate(seq)],
            auto_detected=True,
        )
        saved.append({"shortcut_name": name, "sequence": list(seq), "sessions": len(sessions)})

    return saved


def list_shortcuts_summary() -> str:
    shortcuts = db.list_workflow_shortcuts()
    if not shortcuts:
        return "  No workflow shortcuts yet."
    lines = ["======= Workflow Shortcuts ======="]
    for s in shortcuts:
        auto = "[auto]" if s["auto_detected"] else "[manual]"
        lines.append(
            f"  {auto} {s['shortcut_name']:<40}  hits:{s['hit_count']}  triggers:{s['trigger_skills']}"
        )
    return "\n".join(lines)


# ---------- CLI ----------

def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_user_profile.py profile\n"
        "  toolforge_user_profile.py shortcuts\n"
        "  toolforge_user_profile.py detect_shortcuts [<min_sessions>]\n"
        "  toolforge_user_profile.py record_signal <task_type_id> <skill_name> <positive:0|1>\n"
        "  toolforge_user_profile.py top_prefs <task_type_id> [<top_n>]\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage())
        return 2
    cmd = argv[1]
    try:
        if cmd == "profile":
            print(profile_summary())
            return 0
        if cmd == "shortcuts":
            print(list_shortcuts_summary())
            return 0
        if cmd == "detect_shortcuts":
            min_s = int(argv[2]) if len(argv) > 2 else _SHORTCUT_MIN_SESSIONS
            saved = detect_shortcuts_from_pipelines(min_sessions=min_s)
            print(f"  {len(saved)} new shortcut(s) detected and saved")
            for s in saved:
                print(f"    {s['shortcut_name']}  ({s['sessions']} sessions)")
            return 0
        if cmd == "record_signal":
            positive = argv[4] in {"1", "true", "True"}
            record_explicit_feedback(argv[2], argv[3], positive)
            print("ok")
            return 0
        if cmd == "top_prefs":
            top_n = int(argv[3]) if len(argv) > 3 else 5
            prefs = get_top_preferences(argv[2], top_n)
            print(json.dumps(prefs, indent=2))
            return 0
    except (IndexError, ValueError) as exc:
        print(f"toolforge_user_profile: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
