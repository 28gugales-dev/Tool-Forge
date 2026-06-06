#!/usr/bin/env python3
"""ToolForge rate wrapper. Accepts rating via argv to prevent shell interpolation
in the /toolforge-rate slash command. Validates ^[1-5]$ before touching the DB.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import toolforge_db  # noqa: E402

RATING_RE = re.compile(r"^[1-5]$")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: toolforge_rate.py <1-5>", file=sys.stderr)
        return 2
    raw = argv[1].strip()
    if not RATING_RE.match(raw):
        print(
            f"toolforge_rate: invalid rating {raw!r} (must be 1, 2, 3, 4, or 5)",
            file=sys.stderr,
        )
        return 2
    try:
        tool, msg, code = toolforge_db.rate_last(int(raw))
    except ValueError as exc:
        print(f"toolforge_rate: invalid value: {exc}", file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError) as exc:
        print(f"toolforge_rate: db error: {exc}", file=sys.stderr)
        return 3
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
