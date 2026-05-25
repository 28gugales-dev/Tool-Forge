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

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


MAX_STDIN = 1024 * 1024  # 1 MiB


def _counter_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if not safe:
        safe = hashlib.sha1(
            session_id.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"toolforge_session_{safe}.count"


def _prune_stale(tmpdir: Path, age_days: int = 7) -> None:
    # Best-effort cleanup of abandoned counter files. Wrapped to never block
    # the hot path: any OSError (permission, race, missing dir) is swallowed.
    cutoff = time.time() - age_days * 86400
    try:
        for p in tmpdir.glob("toolforge_session_*.count"):
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

    session_id = str(
        event.get("session_id")
        or event.get("sessionId")
        or os.environ.get("CLAUDE_SESSION_ID", "default")
    )

    path = _counter_path(session_id)
    _prune_stale(path.parent)
    # Windows allows concurrent O_APPEND opens to fail with sharing violations,
    # so retry briefly. Linear backoff is enough for ~tens of parallel hooks.
    last_exc: OSError | None = None
    for attempt in range(8):
        try:
            with open(path, "ab") as fh:
                fh.write(b".")
            return 0
        except OSError as exc:
            last_exc = exc
            time.sleep(0.005 * (attempt + 1))
    if last_exc is not None:
        # Exit non-zero so Claude Code surfaces the failure in the transcript.
        # Silent exit 0 would let SessionEnd silently see count=0 and skip the
        # Likert prompt, masking a broken core feedback loop.
        print(
            f"toolforge counter: write failed after retries: {last_exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
