#!/usr/bin/env python3
"""ToolForge Bridge Server — REST API + webhook bridge on port 7842.

Runs independently from the webui (port 7321). Exposes ToolForge state to
external agents (Hermes), note-taking tools (Obsidian), and custom webhooks.

Start with:
  python webui/bridge_server.py [--port 7842] [--bind 127.0.0.1]

Endpoints:
  GET  /api/health             liveness + DB version
  GET  /api/profile            user preference profile (all task types)
  GET  /api/skills             installed skills list (from DB usage_stats)
  GET  /api/stacks             all skill stacks
  GET  /api/pipelines          recent pipelines
  GET  /api/shortcuts          workflow shortcuts
  GET  /api/export/context     full context bundle (profile + stacks + shortcuts)
  POST /api/context/ingest     receive external context (stored in context_sync)
  POST /api/webhooks/hermes    Hermes pushes context to ToolForge
  POST /api/webhooks/obsidian  Obsidian pushes note data to ToolForge
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import toolforge_db as db

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from integrations import hermes as _hermes
    from integrations import obsidian as _obsidian
    _INTEGRATIONS_OK = True
except ImportError:
    _INTEGRATIONS_OK = False

_VERSION = "0.4.0"
_MAX_BODY = 256 * 1024  # 256 KB


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default Apache-style logging

    def _send_json(self, code: int, body: Any) -> None:
        data = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length > _MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(min(length, _MAX_BODY))
        return json.loads(raw.decode("utf-8")) if raw else {}

    # ------ routing ------

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/health":
                return self._get_health()
            if path == "/api/profile":
                return self._get_profile()
            if path == "/api/skills":
                return self._get_skills()
            if path == "/api/stacks":
                return self._get_stacks()
            if path == "/api/pipelines":
                return self._get_pipelines()
            if path == "/api/shortcuts":
                return self._get_shortcuts()
            if path == "/api/export/context":
                return self._get_context_export()
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._read_body()
            if path == "/api/context/ingest":
                return self._post_ingest(body)
            if path == "/api/webhooks/hermes":
                return self._webhook_hermes(body)
            if path == "/api/webhooks/obsidian":
                return self._webhook_obsidian(body)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})
        self._send_json(404, {"error": "not found"})

    # ------ GET handlers ------

    def _get_health(self):
        db.init_db()
        conn = db._connect()
        try:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self._send_json(200, {
            "status": "ok",
            "bridge_version": _VERSION,
            "db_schema_version": ver,
            "integrations_available": _INTEGRATIONS_OK,
        })

    def _get_profile(self):
        prefs = db.get_all_preferences()
        self._send_json(200, {"preferences": prefs})

    def _get_skills(self):
        db.init_db()
        conn = db._connect()
        try:
            rows = conn.execute(
                "SELECT tool_key, count_30d, last_used_at FROM usage_stats ORDER BY count_30d DESC LIMIT 100"
            ).fetchall()
        finally:
            conn.close()
        skills = [{"tool_key": r[0], "count_30d": r[1], "last_used_at": r[2]} for r in rows]
        self._send_json(200, {"skills": skills})

    def _get_stacks(self):
        stacks = db.list_skill_stacks()
        self._send_json(200, {"stacks": stacks})

    def _get_pipelines(self):
        pipes = db.get_recent_pipelines(limit=20)
        self._send_json(200, {"pipelines": pipes})

    def _get_shortcuts(self):
        shortcuts = db.list_workflow_shortcuts()
        self._send_json(200, {"shortcuts": shortcuts})

    def _get_context_export(self):
        """Bundle everything useful for an external agent."""
        prefs = db.get_all_preferences()
        stacks = db.list_skill_stacks()
        shortcuts = db.list_workflow_shortcuts()
        top_predicted = db.get_top_predicted_skills(10)
        self._send_json(200, {
            "source": "toolforge",
            "version": _VERSION,
            "preferences": prefs,
            "stacks": stacks,
            "shortcuts": shortcuts,
            "top_predicted_skills": top_predicted,
        })

    # ------ POST handlers ------

    def _post_ingest(self, body: dict):
        integration = body.get("source", "generic")
        payload_str = json.dumps(body, sort_keys=True)
        from hashlib import sha256
        h = sha256(payload_str.encode()).hexdigest()[:16]
        db.log_context_sync(integration, "pull", payload_hash=h, status="ok")
        self._send_json(200, {"status": "ingested", "hash": h})

    def _webhook_hermes(self, body: dict):
        context = body.get("context", {})
        memories = body.get("memories", [])
        db.log_context_sync("hermes", "pull", status="ok")
        self._send_json(200, {
            "status": "ok",
            "received_memories": len(memories),
            "context_keys": list(context.keys()) if isinstance(context, dict) else [],
        })

    def _webhook_obsidian(self, body: dict):
        note_path = body.get("path", "")
        db.log_context_sync("obsidian", "pull", status="ok")
        self._send_json(200, {"status": "ok", "note_path": note_path})


def run(host: str = "127.0.0.1", port: int = 7842) -> None:
    db.init_db()
    server = HTTPServer((host, port), _Handler)
    print(f"ToolForge bridge server listening on http://{host}:{port}")
    print("  GET  /api/health, /api/profile, /api/skills, /api/stacks, /api/pipelines")
    print("  GET  /api/shortcuts, /api/export/context")
    print("  POST /api/context/ingest, /api/webhooks/hermes, /api/webhooks/obsidian")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="ToolForge bridge server")
    parser.add_argument("--port", type=int, default=7842)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()
    run(args.bind, args.port)


if __name__ == "__main__":
    main()
