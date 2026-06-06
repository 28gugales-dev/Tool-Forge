#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""ToolForge PostToolUse counter. Atomic append-only: one byte per call.

Each call appends a single byte to the per-session file. Single-byte appends
are atomic under O_APPEND on POSIX and FILE_APPEND_DATA on Windows, so parallel
PostToolUse fires cannot lose increments. SessionEnd reads the file size to
get the call count.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path


MAX_STDIN = 1024 * 1024  # 1 MiB
_RETRY_DELAYS_MS = [25, 50, 100, 200]  # exponential cap at 200ms
_PRUNE_SAMPLE_RATE = 0.01  # 1 in 100 PostToolUse invocations


def _counter_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if not safe:
        safe = hashlib.sha1(
            session_id.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"toolforge_session_{safe}.count"


# WARN: see SKETCHY_CODE_AUDIT.md#s2-8 — FIXED in F14 (active path skipped via samefile guard). FIXED in F15 (sampled at 1%).
def _prune_stale(tmpdir: Path, active_path: Path | None = None, age_days: int = 7) -> None:
    # Best-effort cleanup of abandoned counter files. Wrapped to never block
    # the hot path: any OSError (permission, race, missing dir) is swallowed.
    cutoff = time.time() - age_days * 86400
    try:
        for p in tmpdir.glob("toolforge_session_*.count"):
            try:
                if active_path is not None and p.samefile(active_path):
                    continue
            except OSError:
                pass
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        return


def main() -> int:
    raw = sys.stdin.read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        print("toolforge: stdin exceeded 1 MiB cap", file=sys.stderr)
        return 1
    if not raw.strip():
        print("toolforge counter: empty stdin from hook event", file=sys.stderr)
        return 0

    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Exit non-zero so Claude Code surfaces the failure in the transcript.
        # Silent exit 0 would let SessionEnd silently see count=0 and skip the
        # Likert prompt, masking a broken core feedback loop.
        print(f"toolforge counter: bad JSON on stdin: {exc}", file=sys.stderr)
        return 1

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
    # FIXED in F15: sampled at _PRUNE_SAMPLE_RATE to avoid running on every tool call.
    if random.random() < _PRUNE_SAMPLE_RATE:
        _prune_stale(path.parent, path)
    # Windows allows concurrent O_APPEND opens to fail with sharing violations
    # (typically AV / search-indexer holding the file mid-scan). Direct os.open
    # + os.write bypasses Python's buffered file-object setup overhead and uses
    # the kernel-level FILE_APPEND_DATA semantics for atomic append.
    # WARN: see SKETCHY_CODE_AUDIT.md#s2-6 — FIXED in F12 (os.open+os.write, 375ms exp-backoff budget crosses typical AV scan window).
    for delay_ms in _RETRY_DELAYS_MS + [None]:
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            try:
                os.write(fd, b".")
            finally:
                os.close(fd)
            return 0
        except OSError as exc:
            if delay_ms is None:
                # Exit non-zero so Claude Code surfaces the failure in the transcript.
                # Silent exit 0 would let SessionEnd silently see count=0 and skip the
                # Likert prompt, masking a broken core feedback loop.
                print(
                    f"toolforge_post_tool_use_counter: open failed: {exc}",
                    file=sys.stderr,
                )
                return 1
            time.sleep(delay_ms / 1000.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
