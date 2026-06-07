#!/usr/bin/env python3
"""ToolForge <-> Obsidian integration via Obsidian Local REST API plugin.

Writes daily session notes to the user's vault so every Claude Code session
is automatically documented. Also reads existing notes for context retrieval.

Install the "Local REST API" community plugin in Obsidian, then set:

Config keys in ~/.claude/toolforge-config.json:
  "obsidian_base_url": "https://127.0.0.1:27123"  (or http on port 27124)
  "obsidian_api_key": "<your Local REST API key>"
  "obsidian_vault_folder": "ToolForge Sessions"     (default)

The plugin exposes:
  GET  /vault/{path}        read a note
  PUT  /vault/{path}        create/overwrite a note
  POST /vault/{path}        append to a note
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
import toolforge_db as db

_DEFAULT_BASE = "https://127.0.0.1:27123"
_DEFAULT_FOLDER = "ToolForge Sessions"
_TIMEOUT_S = 4

# Obsidian Local REST API uses a self-signed cert — we must disable SSL verify
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _cfg() -> dict:
    try:
        cfg_path = Path.home() / ".claude" / "toolforge-config.json"
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _base_url() -> str:
    return _cfg().get("obsidian_base_url", _DEFAULT_BASE).rstrip("/")


def _api_key() -> Optional[str]:
    return _cfg().get("obsidian_api_key")


def _vault_folder() -> str:
    return _cfg().get("obsidian_vault_folder", _DEFAULT_FOLDER)


def _vault_request(method: str, vault_path: str, body: Optional[str] = None) -> tuple[bool, str]:
    url = _base_url() + "/vault/" + urllib.parse.quote(vault_path, safe="/")
    headers = {"Content-Type": "text/markdown"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body_bytes = body.encode("utf-8") if body else None
    try:
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return True, text
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


# ---------- public API ----------

def write_session_note(session_id: str, task_type: str, skills_used: list[str],
                        summary: str = "", append: bool = True) -> bool:
    """Write (or append to) the daily session note in the Obsidian vault."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note_path = f"{_vault_folder()}/{today}.md"

    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    skills_str = ", ".join(skills_used) if skills_used else "none"

    block = (
        f"\n## Session {session_id[:8]} — {timestamp}\n"
        f"**Task type:** {task_type}\n"
        f"**Skills used:** {skills_str}\n"
    )
    if summary:
        block += f"\n{summary}\n"

    method = "POST" if append else "PUT"
    ok, msg = _vault_request(method, note_path, block)

    payload_hash = sha256(block.encode()).hexdigest()[:16]
    db.log_context_sync(
        "obsidian", "push",
        payload_hash=payload_hash,
        status="ok" if ok else "error",
        error_msg=None if ok else msg[:200],
    )
    return ok


def read_note(vault_relative_path: str) -> Optional[str]:
    """Read a note from the vault. Returns None on failure."""
    ok, content = _vault_request("GET", vault_relative_path)
    db.log_context_sync(
        "obsidian", "pull",
        status="ok" if ok else "error",
        error_msg=None if ok else content[:200],
    )
    return content if ok else None


def get_today_note() -> Optional[str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return read_note(f"{_vault_folder()}/{today}.md")


def health_check() -> dict:
    """Check if the Obsidian REST API is reachable."""
    url = _base_url() + "/obsidian-local-rest-api.json"
    try:
        req = urllib.request.Request(url)
        key = _api_key()
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode(errors="replace"))
            return {"reachable": True, "response": data}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def render_sync_status() -> str:
    history = db.get_sync_history("obsidian", limit=10)
    if not history:
        return "  No Obsidian sync events recorded yet."
    lines = ["======= Obsidian Sync History (last 10) ======="]
    for h in history:
        err = f"  ERR: {h['error_msg']}" if h["error_msg"] else ""
        lines.append(f"  {h['synced_at']}  {h['direction']:<5}  {h['status']}{err}")
    return "\n".join(lines)
