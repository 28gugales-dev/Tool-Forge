#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""UserPromptSubmit hook — first-message predictor.

On the very first message of a session, runs the ToolForge predictor engine
to surface which skills are likely needed. In shadow mode (default) the
predictions are logged but not injected. In active mode a compact
<system-reminder> nudge is injected with the top 2-3 predictions.

Only fires once per session (tracked via a per-session flag file) to avoid
repeated prediction noise on every message.

Config keys in ~/.claude/toolforge-config.json:
  "predictor_mode": "shadow" (default) | "active"
  "predictor_min_confidence": 0.3 (default)
  "predictor_top_n": 3 (default)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PLUGIN_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
CONFIG_PATH = CLAUDE_HOME / "toolforge-config.json"
LOG_PATH = CLAUDE_HOME / "toolforge_predictor.log"

MAX_STDIN = 64 * 1024
WALL_BUDGET_MS = 150   # Prediction is heavier than routing; still fast


def _read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _session_flag_path(session_id: str) -> Path:
    tmpdir = Path(tempfile.gettempdir())
    return tmpdir / f"toolforge_pred_{session_id}.done"


def _already_fired(session_id: str) -> bool:
    return _session_flag_path(session_id).exists()


def _mark_fired(session_id: str) -> None:
    try:
        _session_flag_path(session_id).write_bytes(b"1")
    except OSError:
        pass


def _log(entry: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _build_injection(predictions: list[dict], min_conf: float) -> str:
    strong = [p for p in predictions if p["confidence"] >= min_conf]
    if not strong:
        return ""
    names = ", ".join(p["skill"] for p in strong[:3])
    body = (
        f"ToolForge predicts these skills may be useful this session: {names}. "
        "Run /toolforge-predict for details or /toolforge-hunt <skill> to install any missing ones."
    )
    tag = f"<system-reminder>{body}</system-reminder>"
    return tag[:500]


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
        return 0

    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0

    session_id = event.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "unknown")
    prompt = event.get("message") or event.get("prompt") or ""

    # Only run once per session
    if _already_fired(session_id):
        return 0

    cfg = _read_config()
    mode = cfg.get("predictor_mode", "shadow")
    min_conf = float(cfg.get("predictor_min_confidence", 0.3))
    top_n = int(cfg.get("predictor_top_n", 3))

    start = time.monotonic()
    predictions: list[dict] = []
    error = ""
    try:
        import toolforge_predictor  # type: ignore
        predictions = toolforge_predictor.predict_and_log(session_id, prompt, top_n=top_n)
    except Exception as exc:
        error = str(exc)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    _log({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "predictions": predictions,
        "elapsed_ms": elapsed_ms,
        "mode": mode,
        "error": error or None,
    })

    _mark_fired(session_id)

    if mode == "active" and predictions:
        injection = _build_injection(predictions, min_conf)
        if injection:
            _emit_injection(injection)

    return 0


if __name__ == "__main__":
    sys.exit(main())
