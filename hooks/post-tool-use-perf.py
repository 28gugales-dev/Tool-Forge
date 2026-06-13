#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""ToolForge PostToolUse perf capture. Records one skill_performance sample per
invocation that can be attributed to a known installed skill/tool.

Mirrors post-tool-use-counter.py's discipline: read stdin JSON contract, swallow
all exceptions and exit 0 so a hot-path failure never blocks or slows the session,
hard wall-clock budget, Windows-safe. The counter is fire-and-forget byte append;
this hook does one tiny attributed DB write, still under a strict time budget.

Attribution: only the Skill tool carries a ToolForge skill name (tool_input.skill
or tool_input.command — the `/foo` slash form). Any other tool, or a Skill event
whose name is not a known *approved install*, is silently skipped — matching the
"if attribution impossible, skip" rule. Unknown skills are never invented.

Always exits 0. Never writes to stderr in the success path (the counter exits
non-zero on a broken core loop; perf capture is best-effort enrichment, so a
miss here is benign and must stay silent).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MAX_STDIN = 1024 * 1024  # 1 MiB, matching the counter hook
WALL_BUDGET_S = 0.25     # hard budget — abandon silently if the DB is contended

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PLUGIN_ROOT / "bin"


def _resolve_skill_name(event: dict) -> str | None:
    """Extract a ToolForge skill name from a Skill-tool PostToolUse event.

    Returns None when the event is not a Skill invocation or carries no name —
    the caller skips silently in that case (attribution impossible)."""
    tool_name = event.get("tool_name") or event.get("toolName") or ""
    if tool_name != "Skill":
        return None
    tool_input = event.get("tool_input") or event.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return None
    # Claude Code's Skill tool passes the skill under "skill"; the slash form
    # ("/foo args") may arrive under "command". Accept either, take the head.
    raw = tool_input.get("skill") or tool_input.get("command") or ""
    if not isinstance(raw, str):
        return None
    name = raw.strip().lstrip("/").split()[0] if raw.strip() else ""
    return name or None


def _was_success(event: dict) -> bool:
    """Best-effort success signal from the tool response. Absent/ambiguous -> success."""
    resp = event.get("tool_response") or event.get("toolResponse")
    if isinstance(resp, dict):
        # Claude Code marks failed tool calls with is_error/isError on the response.
        if resp.get("is_error") or resp.get("isError"):
            return False
    return True


def main() -> int:
    start = time.monotonic()
    try:
        raw = sys.stdin.read(MAX_STDIN + 1)
        if len(raw) > MAX_STDIN or not raw.strip():
            return 0

        event = json.loads(raw)

        skill_name = _resolve_skill_name(event)
        if not skill_name:
            return 0  # attribution impossible — skip silently

        if str(BIN_DIR) not in sys.path:
            sys.path.insert(0, str(BIN_DIR))
        import toolforge_db as db  # imported lazily so a missing bin/ never blocks

        # Only record skills that are an approved install. A name we can't attribute
        # to the install ledger is dropped — never invent a skill.
        try:
            name = db._validate_tool_name(skill_name)
        except ValueError:
            return 0
        if not _is_installed(db, name):
            return 0

        if time.monotonic() - start > WALL_BUDGET_S:
            return 0  # blew the budget before the write — abandon, never block

        # No per-tool duration in the event; record a neutral latency sample so the
        # EMA is unaffected while error_count/success_count (what auto-retire reads)
        # accumulate real data. token_count=0 leaves token_avg untouched.
        db.upsert_skill_performance(name, latency_ms=0.0, success=_was_success(event))
    except Exception:
        # Best-effort enrichment — any failure (bad JSON, locked DB, import error,
        # timeout) is swallowed so the session never stalls.
        return 0
    return 0


def _is_installed(db, name: str) -> bool:
    """True if `name` has an approved install row. Direct query (no dedicated
    helper exists), mirroring purge_stale / session-end-learner's _connect usage."""
    db.init_db()
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM installs WHERE tool_name=? AND approved=1 LIMIT 1",
            (name,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


if __name__ == "__main__":
    sys.exit(main())
