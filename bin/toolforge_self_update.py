#!/usr/bin/env python3
"""ToolForge self-update checker. Keeps the ToolForge plugin itself current.

Distinct from toolforge_update_checker.py, which scans the health of the user's
*installed tools*. This module checks ToolForge's OWN version against upstream
and, when a newer release exists, fast-forward pulls it in.

Flow (driven by the user-prompt-self-update.py hook):
  1. hook surfaces the last run's result (notice), then
  2. if the 24h TTL has elapsed, spawns `run` in a detached background process.

`run` does the slow work off the prompt's critical path:
  - GET raw plugin.json from upstream main (host on ToolForge's allow-list)
  - semver-compare against the local plugin.json version
  - if remote is newer AND the working tree is clean: `git pull --ff-only`
  - persist the outcome to ~/.claude/toolforge_self_update.json

Every failure degrades silently to "notify only" — no network, not a git repo,
a dirty tree, or a non-fast-forward divergence never crashes and never clobbers
local changes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
STATE_PATH = CLAUDE_HOME / "toolforge_self_update.json"
CHECK_TTL = 24 * 3600  # check upstream at most once per day

REMOTE_VERSION_URL = (
    "https://raw.githubusercontent.com/28gugales-dev/Tool-Forge/"
    "main/.claude-plugin/plugin.json"
)
HTTP_TIMEOUT = 5.0
USER_AGENT = "ToolForge-self-update"


# ---------- version helpers ----------

def _parse_version(s: str | None) -> tuple[int, ...]:
    """Parse 'x.y.z' (ignoring any '-prerelease'/'+build' suffix) to a tuple.

    Non-numeric or missing components degrade to 0 so comparison never raises.
    """
    if not s:
        return (0,)
    core = s.strip().split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for chunk in core.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def _version_lt(a: str | None, b: str | None) -> bool:
    """True if version a is strictly older than version b."""
    return _parse_version(a) < _parse_version(b)


def _read_plugin_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _local_version(root: Path) -> str | None:
    try:
        return _read_plugin_json(root / ".claude-plugin" / "plugin.json").get("version")
    except (OSError, json.JSONDecodeError):
        return None


def _fetch_remote_version() -> str | None:
    req = urllib.request.Request(REMOTE_VERSION_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (fixed allow-listed host)
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("version")


# ---------- git helpers ----------

def _git(root: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _is_git_repo(root: Path) -> bool:
    try:
        cp = _git(root, "rev-parse", "--is-inside-work-tree", timeout=5.0)
        return cp.returncode == 0 and cp.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _is_dirty(root: Path) -> bool:
    """True if the working tree has uncommitted changes (so we must NOT pull)."""
    try:
        cp = _git(root, "status", "--porcelain", timeout=5.0)
        return bool(cp.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return True  # unknown == treat as dirty; never pull blindly


# ---------- state ----------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def is_due(state: dict | None = None) -> bool:
    """True if no successful check within CHECK_TTL."""
    state = state if state is not None else _load_state()
    ts = state.get("checked_epoch")
    if not isinstance(ts, (int, float)):
        return True
    return (time.time() - ts) > CHECK_TTL


# ---------- core ----------

def run_update(root: Path, monotonic_now: float | None = None) -> dict:
    """Fetch upstream version, compare, and fast-forward pull if safe.

    Returns the freshly-written state dict. Stamps checked_epoch on every call
    that reaches the network so the TTL advances even when already current.
    """
    state: dict = {
        "checked_at": _now(),
        "checked_epoch": time.time(),
        "local_version": _local_version(root),
        "remote_version": None,
        "update_available": False,
        "updated": False,
        "notice_shown": False,
        "message": "",
        "error": None,
    }

    try:
        remote = _fetch_remote_version()
    except Exception as exc:  # network/parse — keep prior state's TTL fresh, notify nothing
        state["error"] = f"fetch_failed: {exc}"
        state["message"] = ""
        _save_state(state)
        return state

    state["remote_version"] = remote
    local = state["local_version"]

    if not _version_lt(local, remote):
        state["message"] = ""  # already current — nothing to surface
        _save_state(state)
        return state

    # A newer version exists.
    state["update_available"] = True

    if not _is_git_repo(root):
        state["message"] = (
            f"ToolForge {remote} available (you have {local}). "
            "This install is not a git checkout — update via /plugin."
        )
        _save_state(state)
        return state

    if _is_dirty(root):
        state["message"] = (
            f"ToolForge {remote} available (you have {local}). "
            "Local changes present — auto-update skipped to protect them. "
            "Commit/stash and run /toolforge-update, or pull manually."
        )
        _save_state(state)
        return state

    try:
        cp = _git(root, "pull", "--ff-only")
    except (OSError, subprocess.SubprocessError) as exc:
        state["message"] = f"ToolForge {remote} available — auto-pull errored: {exc}"
        _save_state(state)
        return state

    if cp.returncode == 0:
        state["updated"] = True
        state["message"] = (
            f"ToolForge self-updated {local} → {remote}. "
            "Restart Claude Code (or reload the plugin) to load the new version."
        )
    else:
        # Non-fast-forward / diverged — never force.
        err = (cp.stderr or cp.stdout or "non-fast-forward").strip().splitlines()
        state["message"] = (
            f"ToolForge {remote} available — auto-pull not fast-forward "
            f"({err[-1] if err else 'diverged'}). Update manually."
        )

    _save_state(state)
    return state


def pending_notice() -> str:
    """One-shot notice for the hook to inject. Empties itself after one read."""
    state = _load_state()
    msg = state.get("message") or ""
    if not msg or state.get("notice_shown"):
        return ""
    state["notice_shown"] = True
    _save_state(state)
    return msg


# ---------- self-test ----------

def _self_test() -> int:
    passed = failed = 0

    def check(name: str, cond: bool) -> None:
        nonlocal passed, failed
        if cond:
            print(f"OK: {name}")
            passed += 1
        else:
            print(f"FAIL: {name}")
            failed += 1

    check("parse basic", _parse_version("0.2.0") == (0, 2, 0))
    check("parse prerelease", _parse_version("1.2.3-beta.1") == (1, 2, 3))
    check("parse build", _parse_version("1.2.3+abc") == (1, 2, 3))
    check("parse junk component", _parse_version("1.x.3") == (1, 0, 3))
    check("parse none", _parse_version(None) == (0,))
    check("lt true", _version_lt("0.2.0", "0.3.0"))
    check("lt minor", _version_lt("0.2.9", "0.10.0"))
    check("lt false equal", not _version_lt("0.3.0", "0.3.0"))
    check("lt false newer local", not _version_lt("1.0.0", "0.9.9"))
    check("lt none local treated old", _version_lt(None, "0.1.0"))

    # is_due with fresh / stale / missing epochs (no disk dependency)
    check("due when missing", is_due({}))
    check("due when stale", is_due({"checked_epoch": time.time() - CHECK_TTL - 10}))
    check("not due when fresh", not is_due({"checked_epoch": time.time()}))

    # local version reads this repo's own plugin.json
    lv = _local_version(PLUGIN_ROOT)
    check("local version present", bool(lv))

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- cli ----------

def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_self_update.py run [--root DIR]   # fetch+compare+pull, write state\n"
        "  toolforge_self_update.py check [--root DIR] # print state json (no network)\n"
        "  toolforge_self_update.py notice             # print one-shot notice, then clear\n"
        "  toolforge_self_update.py --self-test\n"
    )


def _root_from_argv(argv: list[str]) -> Path:
    if "--root" in argv:
        i = argv.index("--root")
        if i + 1 < len(argv):
            return Path(argv[i + 1]).resolve()
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    return Path(env).resolve() if env else PLUGIN_ROOT


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage(), file=sys.stderr)
        return 2
    if argv[0] == "--self-test":
        return _self_test()

    if argv[0] == "run":
        state = run_update(_root_from_argv(argv))
        return 0 if not state.get("error") else 1

    if argv[0] == "check":
        print(json.dumps(_load_state(), indent=2))
        return 0

    if argv[0] == "notice":
        n = pending_notice()
        if n:
            print(n)
        return 0

    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
