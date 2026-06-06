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
import secrets
import socket
import subprocess
import sys
import threading
import traceback
import uuid
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

# ---------- section: constants ----------
VERSION = "0.1.0"
DEFAULT_HOST = "127.0.0.1"
# WARN: see SKETCHY_CODE_AUDIT.md#s3-6 — FIXED in F22 (DEFAULT_PORT_RANGE defined here; launch.py imports it).
DEFAULT_PORT = 7321
MAX_PORT_SCAN = 50
DEFAULT_PORT_RANGE = range(DEFAULT_PORT, DEFAULT_PORT + MAX_PORT_SCAN)
MAX_STATIC_BYTES = 20 * 1024 * 1024  # 20 MiB cap on static asset serving

# Per-process CSRF token. Regenerated each server boot; clients fetch via /api/csrf.
CSRF_TOKEN = secrets.token_urlsafe(32)

# Allow-list roots for /api/open. Resolved once at import.
SAFE_ROOTS = (
    FLOWS_DIR.resolve(),
    exporter_mod.USER_SKILLS_DIR.resolve(),
)
# WARN: see SKETCHY_CODE_AUDIT.md#s1-2 — FIXED in F02 (.html removed from set; OS handler can no longer execute JS via planted SKILL.md.html).
# .html intentionally omitted — OS handler executes JS in default browser w/ same-origin access. See SKETCHY_CODE_AUDIT.md#s1-2.
ALLOWED_OPEN_EXTS = frozenset({".md", ".json", ".txt"})

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


# ---------- section: request-handler-class ----------
class Handler(BaseHTTPRequestHandler):
    server_version = f"ToolForgeWebUI/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write(f"[toolforge-ui] {fmt % args}\n")

    def _allowed_origins(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    # ---------- section: security-checks ----------
    def _check_origin(self) -> bool:
        # Origin is required on POST/DELETE (browsers always send it for
        # non-GET cross-origin and same-origin POST). For GET (e.g. the CSRF
        # bootstrap), browsers omit Origin on same-origin requests per Fetch
        # spec, so fall back to a Referer host match. Cross-site reads are
        # still blocked because /api/csrf only sets CORS headers for allowed
        # origins, so a malicious page cannot read the token even if it can
        # trigger the request.
        allowed = self._allowed_origins()
        origin = self.headers.get("Origin")
        if origin:
            return origin in allowed
        referer = self.headers.get("Referer")
        if referer:
            parsed = urlparse(referer)
            referer_origin = f"{parsed.scheme}://{parsed.netloc}"
            return referer_origin in allowed
        # No Origin and no Referer — only safe on GET (same-origin top-level
        # nav from a bookmark, etc.). The do_POST/do_DELETE paths still
        # demand a valid CSRF token, which a cross-site request cannot
        # obtain, so this is safe.
        return self.command == "GET"

    def _check_csrf(self) -> bool:
        token = self.headers.get("X-CSRF-Token", "")
        return bool(token) and secrets.compare_digest(token, CSRF_TOKEN)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin")
        if origin and origin in self._allowed_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()
        self.wfile.write(body)

    def _server_error(self, exc: Exception) -> None:
        err_id = uuid.uuid4().hex
        sys.stderr.write(f"[toolforge-ui] errorId={err_id}\n")
        traceback.print_exc()
        self._send_json({"error": "internal error", "errorId": err_id}, 500)

    def _open_path_ok(self, raw: str) -> tuple[bool, str]:
        if not raw:
            return False, "path missing"
        try:
            target = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return False, "invalid path"
        if not target.exists():
            return False, "path not found"
        if not any(target == root or target.is_relative_to(root) for root in SAFE_ROOTS):
            return False, "path outside allow-list"
        if target.is_file() and target.suffix.lower() not in ALLOWED_OPEN_EXTS:
            return False, "extension not allowed"
        return True, str(target)

    # ---------- section: static-serving ----------
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
        # WARN: see SKETCHY_CODE_AUDIT.md#s2-11 — FIXED in F16 (20 MiB cap returns 413; remaining bytes streamed in 64 KiB chunks via wfile, never fully buffered).
        size = target.stat().st_size
        if size > MAX_STATIC_BYTES:
            self.send_response(413)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with target.open("rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    # ---------- section: route-handlers ----------
    def do_OPTIONS(self) -> None:  # noqa: N802
        origin = self.headers.get("Origin")
        if not origin or origin not in self._allowed_origins():
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
        self.send_header("Access-Control-Allow-Credentials", "true")
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
            if path == "/api/csrf":
                if not self._check_origin():
                    self._send_json({"error": "origin not allowed"}, 403)
                    return
                self._send_json({"token": CSRF_TOKEN})
                return
            if path == "/api/inventory":
                self._send_json(inventory_mod.build_inventory())
                return
            if path == "/api/flows":
                flows = []
                response_warnings: list[dict] = []
                for fp in sorted(FLOWS_DIR.glob("*.json")):
                    try:
                        flows.append(json.loads(fp.read_text(encoding="utf-8")))
                    # WARN: see SKETCHY_CODE_AUDIT.md#s2-3 — FIXED in F09 (corrupt flow files now surface in response_warnings list instead of vanishing silently).
                    except (OSError, json.JSONDecodeError) as exc:
                        response_warnings.append({"file": fp.name, "error": str(exc)})
                        continue
                self._send_json({"flows": flows, "warnings": response_warnings})
                return
            if path.startswith("/api/flows/"):
                trigger = path[len("/api/flows/"):]
                fp = _flow_path(trigger)
                if not fp.exists():
                    self._send_json({"error": "flow not found"}, 404)
                    return
                # WARN: see SKETCHY_CODE_AUDIT.md#s2-3 — FIXED in F09 (corrupt flow JSON now returns 422 with file+detail instead of opaque 500).
                try:
                    flow_payload = json.loads(fp.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    self._send_json(
                        {"error": "flow file corrupted", "file": str(fp), "detail": str(exc)},
                        422,
                    )
                    return
                self._send_json(flow_payload)
                return
            if path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
                return
            if path == "/favicon.ico":
                self._send_static("favicon.svg")
                return
            self.send_error(HTTPStatus.NOT_FOUND, f"unknown route: {path}")
        except Exception as exc:  # noqa: BLE001
            self._server_error(exc)

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            if not self._check_origin():
                self._send_json({"error": "origin not allowed"}, 403)
                return
            if not self._check_csrf():
                self._send_json({"error": "csrf token invalid"}, 403)
                return
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/flows/"):
                trigger = path[len("/api/flows/"):]
                fp = _flow_path(trigger)
                removed_local = False
                if fp.exists():
                    fp.unlink()
                    removed_local = True
                removed_skill, skill_errors = exporter_mod.delete_exported_flow(trigger)
                if not removed_skill:
                    self._send_json(
                        {
                            "ok": False,
                            "removed_local": removed_local,
                            "removed_skill": False,
                            "errors": skill_errors,
                        },
                        500,
                    )
                    return
                self._send_json({"ok": True, "removed_local": removed_local, "removed_skill": True})
                return
            self.send_error(HTTPStatus.NOT_FOUND, f"unknown route: {path}")
        except Exception as exc:  # noqa: BLE001
            self._server_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self._check_origin():
                self._send_json({"error": "origin not allowed"}, 403)
                return
            if not self._check_csrf():
                self._send_json({"error": "csrf token invalid"}, 403)
                return
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
            if path == "/api/preview":
                try:
                    rendered = exporter_mod.render_skill_md(body)
                    self._send_json({
                        "ok": True,
                        "markdown": rendered["markdown"],
                        "steps": rendered["steps"],
                        "trigger": rendered["trigger"],
                        "skill_slug": rendered["skill_slug"],
                    })
                except exporter_mod.FlowCycleError as cyc:
                    self._send_json(
                        {"ok": False, "error": "cycle", "cycle": cyc.cycle,
                         "message": str(cyc)},
                        409,
                    )
                except ValueError as ve:
                    self._send_json(
                        {"ok": False, "error": "invalid_flow", "message": str(ve)},
                        400,
                    )
                return
            if path == "/api/export":
                try:
                    result = exporter_mod.export_flow(body)
                except exporter_mod.FlowCycleError as cyc:
                    self._send_json(
                        {"ok": False, "error": "cycle", "cycle": cyc.cycle,
                         "message": str(cyc)},
                        409,
                    )
                    return
                except ValueError as ve:
                    self._send_json(
                        {"ok": False, "error": "invalid_flow", "message": str(ve)},
                        400,
                    )
                    return
                self._send_json(result)
                return
            # WARN: see SKETCHY_CODE_AUDIT.md#s1-2 — FIXED in F02 (.html dropped from ALLOWED_OPEN_EXTS; _open_path_ok rejects .html with 403 "extension not allowed").
            if path == "/api/open":
                ok, info = self._open_path_ok(body.get("path", ""))
                if not ok:
                    self._send_json({"ok": False, "error": info}, 403)
                    return
                target = info
                if sys.platform.startswith("win"):
                    os.startfile(target)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", target])  # noqa: S603,S607
                else:
                    subprocess.Popen(["xdg-open", target])  # noqa: S603,S607
                self._send_json({"ok": True})
                return
            self.send_error(HTTPStatus.NOT_FOUND, f"unknown route: {path}")
        except Exception as exc:  # noqa: BLE001
            self._server_error(exc)


# ---------- section: server-bootstrap ----------
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
