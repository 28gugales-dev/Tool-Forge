#!/usr/bin/env python3
"""ToolForge web UI server. Pure stdlib, no pip installs, no permission prompts.

Routes:
    GET  /                         -> static index.html
    GET  /static/<path>            -> CSS/JS/assets
    GET  /api/health               -> {ok, version}
    GET  /api/inventory            -> {items, counts}
    GET  /api/flows                -> [flow, ...]
    GET  /api/flows/<trigger>      -> flow
    POST /api/flows                -> save flow (also writes ./flows/<slug>.json)
    DELETE /api/flows/<trigger>    -> remove saved flow + exported skill
    POST /api/export               -> writes ~/.claude/skills/toolforge-<slug>/SKILL.md
    POST /api/open                 -> reveal a file in OS file manager (best-effort)

Bind: 127.0.0.1 only. Auto-picks next free port starting at 7321.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
FLOWS_DIR = HERE / "flows"
FLOWS_DIR.mkdir(exist_ok=True)

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import inventory as inventory_mod  # noqa: E402
import exporter as exporter_mod  # noqa: E402

VERSION = "0.1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7321
MAX_PORT_SCAN = 50

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _flow_path(trigger: str) -> Path:
    slug = exporter_mod.slugify(trigger.lstrip("/"))
    return FLOWS_DIR / f"{slug}.json"


class Handler(BaseHTTPRequestHandler):
    server_version = f"ToolForgeWebUI/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write(f"[toolforge-ui] {fmt % args}\n")

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_path: str) -> None:
        target = (STATIC_DIR / rel_path.lstrip("/")).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "path traversal")
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, f"not found: {rel_path}")
            return
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/" or path == "":
                self._send_static("index.html")
                return
            if path == "/api/health":
                self._send_json({"ok": True, "version": VERSION, "port": self.server.server_address[1]})
                return
            if path == "/api/inventory":
                self._send_json(inventory_mod.build_inventory())
                return
            if path == "/api/flows":
                flows = []
                for fp in sorted(FLOWS_DIR.glob("*.json")):
                    try:
                        flows.append(json.loads(fp.read_text(encoding="utf-8")))
                    except (OSError, json.JSONDecodeError):
                        continue
                self._send_json({"flows": flows})
                return
            if path.startswith("/api/flows/"):
                trigger = path[len("/api/flows/"):]
                fp = _flow_path(trigger)
                if not fp.exists():
                    self._send_json({"error": "flow not found"}, 404)
                    return
                self._send_json(json.loads(fp.read_text(encoding="utf-8")))
                return
            if path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
                return
            if path == "/favicon.ico":
                self._send_static("favicon.svg")
                return
            self.send_error(HTTPStatus.NOT_FOUND, f"unknown route: {path}")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": str(exc)}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/flows/"):
                trigger = path[len("/api/flows/"):]
                fp = _flow_path(trigger)
                removed_local = False
                if fp.exists():
                    fp.unlink()
                    removed_local = True
                removed_skill = exporter_mod.delete_exported_flow(trigger)
                self._send_json({"ok": True, "removed_local": removed_local, "removed_skill": removed_skill})
                return
            self.send_error(HTTPStatus.NOT_FOUND, f"unknown route: {path}")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            body = self._read_body()
            if path == "/api/flows":
                flow = body
                trigger = (flow.get("trigger") or flow.get("name") or "untitled").lstrip("/")
                flow["trigger"] = exporter_mod.slugify(trigger)
                fp = _flow_path(flow["trigger"])
                fp.write_text(json.dumps(flow, indent=2), encoding="utf-8")
                self._send_json({"ok": True, "path": str(fp), "trigger": flow["trigger"]})
                return
            if path == "/api/export":
                result = exporter_mod.export_flow(body)
                self._send_json(result)
                return
            if path == "/api/open":
                target = body.get("path", "")
                if target and Path(target).exists():
                    if sys.platform.startswith("win"):
                        os.startfile(target)  # type: ignore[attr-defined]
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", target])  # noqa: S603,S607
                    else:
                        subprocess.Popen(["xdg-open", target])  # noqa: S603,S607
                    self._send_json({"ok": True})
                    return
                self._send_json({"ok": False, "error": "path missing"}, 400)
                return
            self.send_error(HTTPStatus.NOT_FOUND, f"unknown route: {path}")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": str(exc), "trace": traceback.format_exc()}, 500)


def find_free_port(host: str, start: int) -> int:
    for offset in range(MAX_PORT_SCAN):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in {start}..{start + MAX_PORT_SCAN}")


def run(host: str = DEFAULT_HOST, port: int | None = None, open_browser: bool = True) -> None:
    port = port or find_free_port(host, DEFAULT_PORT)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"[toolforge-ui] serving {url}")
    print(f"[toolforge-ui] static={STATIC_DIR}")
    print(f"[toolforge-ui] flows={FLOWS_DIR}")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[toolforge-ui] shutdown")
        httpd.shutdown()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    run(args.host, args.port, not args.no_browser)
