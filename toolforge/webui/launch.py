#!/usr/bin/env python3
"""Launcher for /toolforge-ui slash command.

Spawns the server as a detached background process so the slash command returns
immediately, then opens the browser.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
DEFAULT_PORT_RANGE = range(7321, 7371)


def _is_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.3) as r:  # noqa: S310
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def main() -> int:
    for port in DEFAULT_PORT_RANGE:
        if _is_up(port):
            url = f"http://127.0.0.1:{port}/"
            print(f"[toolforge-ui] already running at {url}")
            webbrowser.open(url)
            return 0

    python = sys.executable or "python"
    kwargs: dict = {"cwd": str(HERE)}
    if sys.platform.startswith("win"):
        DETACHED = 0x00000008
        CREATE_NEW_PG = 0x00000200
        kwargs["creationflags"] = DETACHED | CREATE_NEW_PG
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True

    log = HERE / "server.log"
    fh = log.open("ab")
    proc = subprocess.Popen(  # noqa: S603
        [python, str(SERVER), "--no-browser"],
        stdout=fh, stderr=fh, stdin=subprocess.DEVNULL, **kwargs,
    )
    print(f"[toolforge-ui] spawned pid={proc.pid} log={log}")

    for _ in range(40):
        for port in DEFAULT_PORT_RANGE:
            if _is_up(port):
                url = f"http://127.0.0.1:{port}/"
                print(f"[toolforge-ui] ready at {url}")
                webbrowser.open(url)
                return 0
        time.sleep(0.25)

    print("[toolforge-ui] server failed to come up — check server.log", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
