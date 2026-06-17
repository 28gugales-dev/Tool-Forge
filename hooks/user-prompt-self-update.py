#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""UserPromptSubmit hook — ToolForge self-update.

Fast, non-blocking. Two jobs, in order:
  1. Surface the previous background run's result (a one-shot notice injected
     as additionalContext) — e.g. "ToolForge self-updated 0.2.0 -> 0.3.0".
  2. If the 24h check is due, spawn `toolforge_self_update.py run` as a DETACHED
     background process and return immediately. The slow work (network fetch +
     git pull) never blocks the user's prompt; its result shows up next prompt.

Everything is best-effort: any failure exits 0 and stays silent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PLUGIN_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

MAX_STDIN = 64 * 1024


def _emit(context: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


def _spawn_background_run() -> None:
    """Launch the worker detached so it outlives this hook without blocking."""
    script = BIN_DIR / "toolforge_self_update.py"
    if not script.exists():
        return
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(PLUGIN_ROOT),
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survive hook exit, no console.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, str(script), "run", "--root", str(PLUGIN_ROOT)],
            **kwargs,
        )
    except (OSError, ValueError):
        pass


def main() -> int:
    # Drain stdin so the harness pipe never blocks; content itself is unused.
    try:
        raw = sys.stdin.read(MAX_STDIN + 1)
        if len(raw) > MAX_STDIN:
            raw = raw[:MAX_STDIN]
    except OSError:
        raw = ""

    try:
        import toolforge_self_update as su  # type: ignore
    except Exception:
        return 0

    # 1) Surface any pending one-shot notice from a prior background run.
    try:
        notice = su.pending_notice()
        if notice:
            _emit(f"<system-reminder>{notice[:500]}</system-reminder>")
    except Exception:
        pass

    # 2) Kick off a fresh check if due — detached, fire-and-forget.
    try:
        if su.is_due():
            # Stamp the TTL now so we don't respawn a worker every prompt while
            # the first one is still fetching/pulling.
            state = su._load_state()
            state["checked_epoch"] = time.time()
            state["checked_at"] = su._now()
            su._save_state(state)
            _spawn_background_run()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
