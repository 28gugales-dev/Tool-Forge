#!/usr/bin/env python3
"""ToolForge token tracker. Estimates and records token usage per skill per
session so the predictor and ranker can factor in token efficiency.

Token counting uses a heuristic (chars / 4) when actual counts are unavailable.
When integrated with the Anthropic SDK, pass real counts directly.

CLI exit codes: 0 success, 2 usage error, 3 I/O error.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Allow running from bin/ or repo root.
sys.path.insert(0, str(Path(__file__).parent))
import toolforge_db as db


# ---------- token estimation ----------

def estimate_tokens(text: str) -> int:
    """Heuristic: ~4 chars per token (GPT-family average; Claude is similar)."""
    return max(1, len(text) // 4)


def estimate_prompt_tokens(messages: list[str]) -> int:
    """Estimate total prompt tokens from a list of message strings."""
    return sum(estimate_tokens(m) for m in messages)


# ---------- recording ----------

def record(session_id: str, skill_name: str | None,
           prompt_tokens: int, output_tokens: int) -> None:
    """Write one token-stat row and update the rolling skill_performance EMA."""
    db.log_token_stats(session_id, skill_name, prompt_tokens, output_tokens)
    if skill_name:
        total = prompt_tokens + output_tokens
        db.upsert_skill_performance(skill_name, latency_ms=0.0, success=True, token_count=total)


def record_from_text(session_id: str, skill_name: str | None,
                     prompt_text: str, output_text: str) -> dict:
    """Estimate tokens from raw text, record, and return the estimates."""
    p = estimate_tokens(prompt_text)
    o = estimate_tokens(output_text)
    record(session_id, skill_name, p, o)
    return {"prompt_tokens": p, "output_tokens": o, "total_tokens": p + o}


# ---------- reporting ----------

def efficiency_report(top_n: int = 10) -> str:
    """Render a token-efficiency leaderboard for display."""
    rows = db.get_token_efficiency_rank()
    if not rows:
        return "No token data yet. Token stats are collected automatically during sessions."
    lines = [
        "======= Token Efficiency Leaderboard =======",
        f"  {'Skill':<30} {'Sessions':>8} {'Avg Tokens':>12}",
        "  " + "-" * 52,
    ]
    for r in rows[:top_n]:
        lines.append(
            f"  {r['skill_name']:<30} {r['sessions']:>8} {r['avg_tokens']:>12.0f}"
        )
    lines.append("=" * 45)
    return "\n".join(lines)


def get_skill_token_profile(skill_name: str) -> dict:
    perf = db.get_skill_performance(skill_name)
    stats = db.get_token_stats_bulk([skill_name])
    return {
        "skill_name": skill_name,
        "token_avg": perf.get("token_avg", 0) if perf else 0,
        "token_stats": stats.get(skill_name),
        "performance": perf,
    }


# ---------- CLI ----------

def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_token_tracker.py record <session_id> <skill_name|-> <prompt_tok> <output_tok>\n"
        "  toolforge_token_tracker.py record_text <session_id> <skill_name|-> <prompt_text> <output_text>\n"
        "  toolforge_token_tracker.py estimate <text>\n"
        "  toolforge_token_tracker.py report [<top_n>]\n"
        "  toolforge_token_tracker.py profile <skill_name>\n"
        "  toolforge_token_tracker.py efficiency_rank\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    cmd = argv[1]
    try:
        if cmd == "record":
            skill = None if argv[3] == "-" else argv[3]
            record(argv[2], skill, int(argv[4]), int(argv[5]))
            print("ok")
            return 0
        if cmd == "record_text":
            skill = None if argv[3] == "-" else argv[3]
            result = record_from_text(argv[2], skill, argv[4], argv[5])
            print(json.dumps(result))
            return 0
        if cmd == "estimate":
            print(estimate_tokens(" ".join(argv[2:])))
            return 0
        if cmd == "report":
            top_n = int(argv[2]) if len(argv) > 2 else 10
            print(efficiency_report(top_n))
            return 0
        if cmd == "profile":
            print(json.dumps(get_skill_token_profile(argv[2]), indent=2))
            return 0
        if cmd == "efficiency_rank":
            print(json.dumps(db.get_token_efficiency_rank()))
            return 0
    except (IndexError, ValueError) as exc:
        print(f"toolforge_token_tracker: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"toolforge_token_tracker: {exc}", file=sys.stderr)
        return 3
    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
