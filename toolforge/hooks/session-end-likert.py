#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""ToolForge SessionEnd Likert prompt. Fires only when this session crossed THRESHOLD tool calls.

Reads the per-session counter file as a byte-size count (PostToolUse appends
one byte per call). Always cleans up the counter file in a finally block,
even when the rating prompt does not fire.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "bin"))

THRESHOLD = 5
MAX_STDIN = 1024 * 1024  # 1 MiB

# Sentinel distinguishing "db error already emitted" from "no installs yet".
_DB_ERROR = object()


def _emit(event_name: str, message: str) -> int:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": f"ToolForge: {message}",
        }
    }))
    return 0


def _counter_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if not safe:
        safe = hashlib.sha1(
            session_id.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"toolforge_session_{safe}.count"


def _last_installed_tool():
    # ImportError propagates: db module missing is a real bug, surface it.
    import toolforge_db  # type: ignore
    try:
        return toolforge_db.get_last_installed_tool()
    except (sqlite3.Error, OSError) as exc:
        print(f"toolforge likert: db lookup failed: {exc}", file=sys.stderr)
        _emit("SessionEnd", "db unavailable for last-installed lookup, skipping rating prompt")
        return _DB_ERROR


def main() -> int:
    raw = sys.stdin.read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        print("toolforge: stdin exceeded 1 MiB cap", file=sys.stderr)
        return 1

    if not raw.strip():
        return 0

    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"toolforge likert: bad JSON on stdin: {exc}", file=sys.stderr)
        return _emit("SessionEnd", "hook input unreadable, skipping rating prompt")

    # Refuse to write counter without session_id — falling through to "default" caused cross-session counter collision (audit H5).
    # WARN: see SKETCHY_CODE_AUDIT.md#s2-10 — FIXED in F35 (verified: Claude Code hooks pass session_id via stdin JSON only; no env var exists. Docs: https://code.claude.com/docs/en/hooks).
    session_id = event.get("session_id") or event.get("sessionId")
    if not session_id:
        print(
            f"toolforge: hook aborted — no session_id in event payload. Event keys: {list(event.keys())}",
            file=sys.stderr,
        )
        return 1
    session_id = str(session_id)
    path = _counter_path(session_id)

    try:
        # WARN: see SKETCHY_CODE_AUDIT.md#s2-7 — FIXED in F13 (TOCTOU collapsed into single try/except FileNotFoundError).
        try:
            count = path.stat().st_size
        except FileNotFoundError:
            count = 0
        except OSError as exc:
            print(f"toolforge_session_end_likert: stat failed on {path}: {exc}", file=sys.stderr)
            count = 0

        if count < THRESHOLD:
            return 0

        tool = _last_installed_tool()
        if tool is _DB_ERROR:
            # _last_installed_tool already emitted its diagnostic.
            return 0
        if not tool:
            return _emit(
                "SessionEnd",
                "tool-call threshold reached but no installed tool to rate. Run /toolforge <category> first.",
            )

        message = (
            f"\nToolForge: you used {count} tool calls this session.\n"
            f"How was the most recently installed tool ({tool})?\n"
            "Rate it 1 (terrible) to 5 (great):\n"
            "  /toolforge-rate 1\n"
            "  /toolforge-rate 2\n"
            "  /toolforge-rate 3\n"
            "  /toolforge-rate 4\n"
            "  /toolforge-rate 5\n"
        )
        response = {
            "hookSpecificOutput": {
                "hookEventName": "SessionEnd",
                "additionalContext": message,
            }
        }
        print(json.dumps(response))
        return 0
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"toolforge likert: unlink failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
