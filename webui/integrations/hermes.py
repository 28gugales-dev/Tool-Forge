#!/usr/bin/env python3
"""ToolForge <-> Hermes agent integration.

Hermes is a memory/context store agent. ToolForge can:
  - Push session summaries to Hermes so it remembers what tools were used.
  - Pull the latest context from Hermes to inform skill predictions.

Hermes is assumed to expose a simple REST API at a configurable base URL
(default http://localhost:8000). All calls are fire-and-forget with a 3-second
timeout; failures are logged to context_sync but never crash the caller.

Config keys in ~/.claude/toolforge-config.json:
  "hermes_base_url": "http://localhost:8000"
  "hermes_api_key": "<optional bearer token>"
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
from hashlib import sha256
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
import toolforge_db as db

_DEFAULT_BASE = "http://localhost:8000"
_TIMEOUT_S = 3


def _cfg() -> dict:
    try:
        cfg_path = Path.home() / ".claude" / "toolforge-config.json"
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _base_url() -> str:
    return _cfg().get("hermes_base_url", _DEFAULT_BASE).rstrip("/")


def _api_key() -> Optional[str]:
    return _cfg().get("hermes_api_key")


def _post(path: str, payload: dict) -> tuple[bool, str]:
    url = _base_url() + path
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            text = resp.read().decode(errors="replace")
            return True, text
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def _get(path: str) -> tuple[bool, dict | str]:
    url = _base_url() + path
    headers = {}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode(errors="replace"))
            return True, data
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


# ---------- public API ----------

def push_session_summary(session_id: str, task_type: str,
                          skills_used: list[str], notes: str = "") -> bool:
    """Send a session summary to Hermes memory store."""
    payload = {
        "source": "toolforge",
        "session_id": session_id,
        "task_type": task_type,
        "skills_used": skills_used,
        "notes": notes,
    }
    payload_hash = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    ok, msg = _post("/api/memory/ingest", payload)
    db.log_context_sync(
        "hermes", "push",
        payload_hash=payload_hash,
        status="ok" if ok else "error",
        error_msg=None if ok else msg[:200],
    )
    return ok


def pull_context(query: str = "") -> dict:
    """Fetch relevant context from Hermes for the current session."""
    path = "/api/memory/context"
    if query:
        import urllib.parse
        path += "?q=" + urllib.parse.quote(query)
    ok, data = _get(path)
    db.log_context_sync(
        "hermes", "pull",
        status="ok" if ok else "error",
        error_msg=None if ok else str(data)[:200],
    )
    if ok and isinstance(data, dict):
        return data
    return {}


def health_check() -> dict:
    """Return Hermes health status."""
    ok, data = _get("/api/health")
    return {"reachable": ok, "response": data}


def render_sync_status() -> str:
    history = db.get_sync_history("hermes", limit=10)
    if not history:
        return "  No Hermes sync events recorded yet."
    lines = ["======= Hermes Sync History (last 10) ======="]
    for h in history:
        err = f"  ERR: {h['error_msg']}" if h["error_msg"] else ""
        lines.append(f"  {h['synced_at']}  {h['direction']:<5}  {h['status']}{err}")
    return "\n".join(lines)
