#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""UserPromptSubmit hook. Routes user prompt to matching installed skills.

Shadow mode (default): logs decisions to ~/.claude/toolforge_router.log,
  injects nothing into Claude's context.
Active mode: injects <system-reminder> with top-3 skill suggestions.

Flip mode in ~/.claude/toolforge-config.json: {"router_mode": "active"}
Only flip after reviewing 7d of shadow-log data (false-positive rate < 15%).

Wall budget: 80ms hard — returns empty list on timeout, never blocks prompt.
Security: descriptions are charset-filtered and <system-reminder> tags stripped
  before injection to prevent prompt-injection from skill description fields.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PLUGIN_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
CONFIG_PATH = CLAUDE_HOME / "toolforge-config.json"
LOG_PATH = CLAUDE_HOME / "toolforge_router.log"

MAX_STDIN = 256 * 1024   # 256 KiB cap — user messages are small
WALL_BUDGET_MS = 80
DESC_CHAR_LIMIT = 100
INJECT_CHAR_LIMIT = 500

# Allowlist chars for injected description fields (prompt-injection defense).
_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._,;:!?()\-/]")
# Strip any embedded <system-reminder> tags a skill description might contain.
_TAG_RE = re.compile(r"</?system-reminder[^>]*>", re.IGNORECASE)


def _read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sanitize_desc(text: str) -> str:
    text = _TAG_RE.sub("", text)
    return _SAFE_RE.sub("", text).strip()[:DESC_CHAR_LIMIT]


def _build_injection(matches: list[dict]) -> str:
    parts: list[str] = []
    for m in matches:
        desc = _sanitize_desc(m.get("description") or "")
        name = m["name"]
        parts.append(f"{name} ({desc})" if desc else name)
    body = "Skills that may match: " + ", ".join(parts) + ". Invoke via Skill tool if relevant."
    tag = f"<system-reminder>{body}</system-reminder>"
    if len(tag) > INJECT_CHAR_LIMIT:
        tag = tag[:INJECT_CHAR_LIMIT - 20] + "...</system-reminder>"
    return tag


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(entry: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _emit_injection(injection: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": injection,
        }
    }))


def main() -> int:
    raw = sys.stdin.read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        return 0  # Oversized — skip silently; never block

    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0

    # Claude Code sends the user's message text in "message" field.
    prompt = event.get("message") or event.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    cfg = _read_config()
    mode = cfg.get("router_mode", "shadow")

    start = time.monotonic()
    deadline = start + WALL_BUDGET_MS / 1000.0

    matches: list[dict] = []
    error: str = ""
    try:
        import toolforge_router  # type: ignore
        matches = toolforge_router.route(prompt, deadline=deadline)
    except Exception as exc:
        error = str(exc)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    prompt_hash = hashlib.sha256(
        prompt.encode("utf-8", errors="replace")
    ).hexdigest()[:8]

    log_entry: dict = {
        "ts": _now_iso(),
        "prompt_hash": prompt_hash,
        "top_keys": [m["name"] for m in matches],
        "top_scores": [m["score"] for m in matches],
        "would_inject": bool(matches),
        "elapsed_ms": elapsed_ms,
        "mode": mode,
    }
    if error:
        log_entry["error"] = error
    _log(log_entry)

    if mode == "active" and matches:
        _emit_injection(_build_injection(matches))

    return 0  # Always 0 — never block the prompt


if __name__ == "__main__":
    sys.exit(main())
