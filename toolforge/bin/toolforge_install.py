#!/usr/bin/env python3
"""ToolForge installer. Validates an install command against a strict allow-list,
resolves the executable via shutil.which, and runs it with shell=False.

Threat model: install_command originates from WebFetch-summarized GitHub READMEs
or other web sources that may be adversarial. The skill prompt is NOT a trust
boundary. This script is.

Validation strategy (allow-list, not deny-list):
- argv[0] must be one of ALLOWED_FIRST.
- For each head, subsequent tokens must be either FLAG_RE (looks like a flag)
  or PACKAGE_NAME_RE (looks like a package name / version spec).
- For `claude mcp add <server> <cmd...>`, the nested <cmd...> is recursively
  validated against the same rules (server name must be SERVER_NAME_RE).
- Shell metacharacters anywhere in the raw string are rejected before shlex.

Security review is the curator's responsibility; this installer only re-validates the command string.
"""

# WARN: see SKETCHY_CODE_AUDIT.md#s5-5 — FIXED in F42 (stale "earlier drafts" paragraph removed; docstring now current-state-only).

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import toolforge_db  # noqa: E402

# ---------- section: constants-and-regex-gates ----------

ALLOWED_FIRST = {"claude", "npx", "uvx", "npm", "pip", "pipx", "uv"}
CLAUDE_SUBS = {
    "plugin": {"install", "marketplace", "list", "enable", "disable"},
    "mcp": {"add", "remove", "list"},
}
NPM_SUBS = {"install", "i", "add"}
PIP_SUBS = {"install"}
UV_SUBS = {"pip", "tool", "add"}

DENY_CHARS = re.compile(r"[;&|`<>\n\r\t]")
FLAG_RE = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9-]*(=[A-Za-z0-9._@+,-]+)?$")
PACKAGE_NAME_RE = re.compile(
    r"^@?[A-Za-z0-9][A-Za-z0-9._/-]*(@[A-Za-z0-9._+-]+)?$"
)
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

FLAG_DENY = {
    "--registry", "--index-url", "--extra-index-url", "--config",
    "--prefix", "--cache", "--script-shell", "--shell-escape",
    "--ignore-scripts=false", "--unsafe-perm",
}

# Bounds adversarial postinstall hangs (npm/pip/uvx scripts can spin forever).
# 5 minutes is the longest legitimate install we've seen (large MCP servers
# with native deps); anything longer is treated as malicious or wedged.
INSTALL_TIMEOUT_SECONDS = 300


# ---------- section: validators ----------


def _safe_token(t: str) -> Optional[str]:
    """Return None if token is safe, else a short reason category string."""
    if not (FLAG_RE.match(t) or PACKAGE_NAME_RE.match(t)):
        return "unsupported syntax (allowlist regex)"
    if t.startswith("-"):
        key = t.split("=", 1)[0]
        if key in FLAG_DENY or t in FLAG_DENY:
            return "blocked for security (deny-list)"
    return None


def _safe_log(tool_name: str, category: str, approved: bool) -> Optional[str]:
    """Persist install record. Returns None on success, errorId on failure.
    Stderr-prints the full diagnostic (with errorId) so audit drops are visible.
    Callers on the success path should propagate the errorId to stdout so the
    user sees a single coherent signal alongside the non-zero exit code."""
    try:
        toolforge_db.log_install(tool_name, category, approved=approved)
        return None
    except (ValueError, sqlite3.Error, OSError) as exc:
        error_id = secrets.token_hex(4)
        print(
            f"toolforge audit [errorId={error_id}]: log dropped for "
            f"{tool_name} ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return error_id


def _validate_npx_uvx(argv: list[str], head: str) -> None:
    for t in argv[1:]:
        reason = _safe_token(t)
        if reason is not None:
            raise ValueError(f"unsafe token after {head}: {t!r} ({reason})")


def _validate_npm_pip_uv(argv: list[str], head: str, subs: set[str]) -> None:
    if len(argv) < 3:
        raise ValueError(f"{head} command missing subcommand or target")
    if argv[1] not in subs:
        raise ValueError(
            f"{head} subcommand {argv[1]!r} not in allow-list {sorted(subs)}"
        )
    for t in argv[2:]:
        reason = _safe_token(t)
        if reason is not None:
            raise ValueError(
                f"unsafe token in {head} {argv[1]}: {t!r} ({reason})"
            )


def _validate_claude(argv: list[str]) -> None:
    if len(argv) < 3:
        raise ValueError("claude command missing subcommand or action")
    sub = argv[1]
    if sub not in CLAUDE_SUBS:
        raise ValueError(
            f"claude subcommand {sub!r} not in allow-list {sorted(CLAUDE_SUBS)}"
        )
    action = argv[2]
    if action not in CLAUDE_SUBS[sub]:
        raise ValueError(
            f"claude {sub} {action!r} not in allow-list {sorted(CLAUDE_SUBS[sub])}"
        )
    if sub == "plugin" and action == "install":
        if len(argv) < 4 or not PACKAGE_NAME_RE.match(argv[3]):
            raise ValueError(
                f"claude plugin install requires a valid plugin spec, got {argv[3:]!r}"
            )
        for t in argv[4:]:
            reason = _safe_token(t)
            if reason is not None:
                raise ValueError(
                    f"unsafe token in claude plugin install: {t!r} ({reason})"
                )
    elif sub == "mcp" and action == "add":
        if len(argv) < 5:
            raise ValueError(
                "claude mcp add requires server name plus command"
            )
        # WARN: see SKETCHY_CODE_AUDIT.md#s5-4 — FIXED in F41 (comment rewritten to drop stale v0.1 deferral).
        # Options before server name (-e, -H, -s, -t, ...) are not supported. See SKETCHY_CODE_AUDIT.md#s5-4.
        if argv[3].startswith("-"):
            raise ValueError(
                "options before server name not supported in v0.1 "
                "(deferred to v0.2): rewrite without leading flags"
            )
        if not SERVER_NAME_RE.match(argv[3]):
            raise ValueError(f"invalid mcp server name {argv[3]!r}")
        # Support `claude mcp add <name> -- <cmd...>` (preferred per
        # claude mcp add --help) and the implicit-separator form
        # `claude mcp add <name> <cmd...>` for backward compat.
        if argv[4] == "--":
            if len(argv) < 6:
                raise ValueError(
                    "claude mcp add: missing command after '--' separator"
                )
            nested_start = 5
        else:
            nested_start = 4
        nested_head = argv[nested_start]
        if nested_head not in ALLOWED_FIRST:
            raise ValueError(
                f"nested mcp command head {nested_head!r} not in allow-list"
            )
        # Recursively validate the nested command.
        # _validate_head dispatches through _safe_token, so deny-list flags
        # inside the nested payload (e.g. --registry=http://evil) are rejected.
        _validate_head(argv[nested_start:])
    else:
        for t in argv[3:]:
            reason = _safe_token(t)
            if reason is not None:
                raise ValueError(
                    f"unsafe token in claude {sub} {action}: {t!r} ({reason})"
                )


def _validate_head(argv: list[str]) -> None:
    head = argv[0]
    if head not in ALLOWED_FIRST:
        raise ValueError(f"command head {head!r} not in allow-list {sorted(ALLOWED_FIRST)}")
    if head == "claude":
        _validate_claude(argv)
    elif head in {"npx", "uvx"}:
        _validate_npx_uvx(argv, head)
    elif head == "npm":
        _validate_npm_pip_uv(argv, head, NPM_SUBS)
    elif head in {"pip", "pipx"}:
        _validate_npm_pip_uv(argv, head, PIP_SUBS)
    elif head == "uv":
        _validate_npm_pip_uv(argv, head, UV_SUBS)


def _validate(install_command: str) -> list[str]:
    if not install_command.strip():
        raise ValueError("empty install command")
    if DENY_CHARS.search(install_command):
        raise ValueError(f"shell metacharacters present in {install_command!r}")
    if "$(" in install_command or "${" in install_command:
        raise ValueError("variable / command substitution present")
    # POSIX shlex on POSIX, Windows-style on Windows (avoids backslash escape mangling).
    argv = shlex.split(install_command, posix=(os.name != "nt"))
    if not argv:
        raise ValueError("command parsed to empty argv")
    _validate_head(argv)
    return argv


# ---------- section: exec-resolution ----------


def _resolve(argv: list[str]) -> list[str]:
    head = shutil.which(argv[0])
    if not head:
        raise FileNotFoundError(f"executable not on PATH: {argv[0]}")
    resolved = os.path.realpath(head)
    blocked = [
        os.path.realpath(os.path.expanduser("~/.local/bin")),
        os.path.realpath(os.environ.get("LOCALAPPDATA", "") or os.devnull),
        os.path.realpath(os.path.expanduser("~/AppData/Roaming/npm")),
    ]
    if any(resolved.startswith(b + os.sep) for b in blocked if b and b != os.devnull):
        raise FileNotFoundError(f"refusing user-writable executable: {resolved}")
    if (os.sep + "node_modules" + os.sep + ".bin" + os.sep) in resolved:
        raise FileNotFoundError(f"refusing node_modules/.bin: {resolved}")
    return [resolved] + argv[1:]


def _confirm(prompt: str, auto_yes: bool) -> Optional[bool]:
    """Return True if approved, False if user said no, None if no-tty refusal."""
    if auto_yes:
        print(f"{prompt} [auto-yes]")
        return True
    if not sys.stdin.isatty():
        print(
            "toolforge_install: no tty and --yes not set, refusing",
            file=sys.stderr,
        )
        return None
    try:
        answer = input(f"{prompt} [y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


# ---------- section: single-install ----------


def install(
    tool_name: str,
    install_command: str,
    category: str,
    auto_yes: bool = False,
) -> int:
    try:
        argv = _validate(install_command)
    except ValueError as exc:
        print(f"toolforge_install refused: {exc}", file=sys.stderr)
        _safe_log(tool_name, category, approved=False)
        return 2

    print(f"\nToolForge: install '{tool_name}' (category: {category})")
    print(f"Command:   {' '.join(shlex.quote(a) for a in argv)}\n")

    decision = _confirm("Proceed?", auto_yes)
    if decision is None:
        _safe_log(tool_name, category, approved=False)
        return 2
    if not decision:
        _safe_log(tool_name, category, approved=False)
        print("Skipped.")
        return 0

    try:
        resolved = _resolve(argv)
    except FileNotFoundError as exc:
        print(f"toolforge_install: {exc}", file=sys.stderr)
        _safe_log(tool_name, category, approved=False)
        return 1

    print(f"\nRunning: {' '.join(shlex.quote(a) for a in resolved)}\n")
    # WARN: see SKETCHY_CODE_AUDIT.md#s2-4 — FIXED in F06 (timeout=INSTALL_TIMEOUT_SECONDS, TimeoutExpired handled).
    try:
        proc = subprocess.run(
            resolved,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"toolforge_install: subprocess timed out after "
            f"{INSTALL_TIMEOUT_SECONDS}s: {' '.join(resolved)}",
            file=sys.stderr,
        )
        _safe_log(tool_name, category, approved=False)
        return 4
    except OSError as exc:
        print(f"toolforge_install: failed to execute: {exc}", file=sys.stderr)
        _safe_log(tool_name, category, approved=False)
        return 1

    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    if proc.returncode != 0:
        print(
            f"\ntoolforge_install: command exited {proc.returncode}",
            file=sys.stderr,
        )
        _safe_log(tool_name, category, approved=False)
        return 4

    error_id = _safe_log(tool_name, category, approved=True)
    if error_id is not None:
        print(
            f"\ntoolforge: WARNING — install of {tool_name} succeeded but "
            f"audit log FAILED (errorId={error_id}). See stderr for diagnostic. "
            f"Exit code 3."
        )
        return 3
    print(f"\nInstalled {tool_name}. Rate it with: /toolforge-rate <1-5>")
    return 0


# ---------- section: batch-install ----------


def install_batch(items: list[dict], auto_yes: bool) -> int:
    """Validate every install command first (fail-fast), single user confirm,
    then run each install sequentially. Per-tool audit log; aggregate exit code.

    items: [{"tool_name": str, "install_command": str, "category": str}, ...]
    Exit codes: 0 all ok, 2 validation/usage or user-no, 3 audit-log drop on
    success path, 4 any child exited nonzero. Validation refusal logs every
    item (the refused one + all siblings) as approved=False so the audit
    table reflects that the user's batch intent was rejected as a unit.
    """
    if not isinstance(items, list) or not items:
        print("toolforge_install: empty or non-list batch payload", file=sys.stderr)
        return 2

    validated: list[tuple[str, str, list[str]]] = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            print(
                f"toolforge_install: batch item {idx} not an object",
                file=sys.stderr,
            )
            return 2
        name = it.get("tool_name", "")
        cmd = it.get("install_command", "")
        cat = it.get("category", "")
        if not (name and cmd and cat):
            print(
                f"toolforge_install: batch item {idx} missing required field",
                file=sys.stderr,
            )
            return 2
        try:
            argv = _validate(cmd)
        except ValueError as exc:
            print(
                f"toolforge_install batch refused {name!r}: {exc}",
                file=sys.stderr,
            )
            for v_name, v_cat, _ in validated:
                _safe_log(v_name, v_cat, approved=False)
            _safe_log(name, cat, approved=False)
            return 2
        validated.append((name, cat, argv))

    print(f"\nToolForge: batch install ({len(validated)} tool(s)):")
    for name, cat, argv in validated:
        print(f"  - {name} ({cat}): {' '.join(shlex.quote(a) for a in argv)}")
    print()

    decision = _confirm(f"Install all {len(validated)}?", auto_yes)
    if decision is None:
        for name, cat, _ in validated:
            _safe_log(name, cat, approved=False)
        return 2
    if not decision:
        for name, cat, _ in validated:
            _safe_log(name, cat, approved=False)
        print("Skipped.")
        return 0

    any_child_fail = False
    any_audit_fail = False
    for name, cat, argv in validated:
        print(f"\n=== {name} ===")
        try:
            resolved = _resolve(argv)
        except FileNotFoundError as exc:
            print(f"toolforge_install: {exc}", file=sys.stderr)
            _safe_log(name, cat, approved=False)
            any_child_fail = True
            continue
        print(f"Running: {' '.join(shlex.quote(a) for a in resolved)}")
        # WARN: see SKETCHY_CODE_AUDIT.md#s2-4 — FIXED in F06 (timeout=INSTALL_TIMEOUT_SECONDS, TimeoutExpired handled).
        try:
            proc = subprocess.run(
                resolved,
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            print(
                f"toolforge_install: subprocess timed out after "
                f"{INSTALL_TIMEOUT_SECONDS}s: {' '.join(resolved)}",
                file=sys.stderr,
            )
            _safe_log(name, cat, approved=False)
            any_child_fail = True
            continue
        except OSError as exc:
            print(
                f"toolforge_install: failed to execute {name}: {exc}",
                file=sys.stderr,
            )
            _safe_log(name, cat, approved=False)
            any_child_fail = True
            continue
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            print(
                f"toolforge_install: {name} exited {proc.returncode}",
                file=sys.stderr,
            )
            _safe_log(name, cat, approved=False)
            any_child_fail = True
            continue
        error_id = _safe_log(name, cat, approved=True)
        if error_id is not None:
            any_audit_fail = True
            print(
                f"toolforge: WARNING — install of {name} succeeded but "
                f"audit log FAILED (errorId={error_id}). See stderr."
            )
        else:
            print(f"Installed {name}.")

    if any_child_fail:
        return 4
    if any_audit_fail:
        return 3
    print(
        f"\nBatch complete: {len(validated)} tool(s). "
        f"Rate each with: /toolforge-rate <1-5>"
    )
    return 0


def _self_test() -> int:
    """Validate _validate() against representative cases. Exit 0 if all pass."""
    cases: list[tuple[str, str, bool, Optional[str]]] = [
        (
            "explicit -- separator",
            "claude mcp add shadcn-ui -- npx -y @jpisnice/shadcn-ui-mcp-server",
            True, None,
        ),
        (
            "implicit separator (backward compat)",
            "claude mcp add shadcn-ui npx -y @jpisnice/shadcn-ui-mcp-server",
            True, None,
        ),
        (
            "option before server name rejected",
            "claude mcp add -e API_KEY=foo shadcn-ui -- npx -y pkg",
            False, "options before server name not supported in v0.1",
        ),
        (
            "nested head not in allow-list",
            "claude mcp add shadcn -- evilbin",
            False, "not in allow-list",
        ),
        (
            "nested flag with non-allowlist value rejected (FLAG_RE gate)",
            "claude mcp add shadcn-ui -- npx --registry=http://evil pkg",
            False, "--registry",
        ),
        (
            "nested deny-list flag rejected (FLAG_DENY gate)",
            "claude mcp add shadcn-ui -- npx --unsafe-perm pkg",
            False, "blocked for security",
        ),
        (
            "claude plugin install regression",
            "claude plugin install shadcn-mcp@local",
            True, None,
        ),
        (
            "bad server name regex",
            "claude mcp add bad;name -- npx pkg",
            False, "shell metacharacters",
        ),
    ]
    passed = 0
    failed = 0
    for desc, cmd, expect_ok, expect_substr in cases:
        try:
            _validate(cmd)
            got_ok = True
            err_msg = ""
        except ValueError as exc:
            got_ok = False
            err_msg = str(exc)
        if expect_ok and got_ok:
            print(f"OK: {desc}")
            passed += 1
        elif (not expect_ok) and (not got_ok) and (
            expect_substr is None or expect_substr in err_msg
        ):
            print(f"OK: {desc} (rejected: {err_msg})")
            passed += 1
        else:
            if expect_ok and not got_ok:
                reason = f"expected PASS, got rejection: {err_msg}"
            elif (not expect_ok) and got_ok:
                reason = "expected FAIL, but validation passed"
            else:
                reason = (
                    f"expected substring {expect_substr!r} in error, got: {err_msg}"
                )
            print(f"FAIL: {desc}: {reason}")
            failed += 1
    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- section: cli-dispatcher ----------


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return _self_test()
    auto_yes = "--yes" in argv
    is_batch = "--batch" in argv
    rest = [a for a in argv if a not in {"--yes", "--batch"}]
    if is_batch:
        # Stdin JSON: [{"tool_name":..,"install_command":..,"category":..}, ...]
        # WARN: see SKETCHY_CODE_AUDIT.md#s5-1 — FIXED in F38 (reference now points to ARCHITECTURE.md §7).
        # 1 MiB cap mirrors the hook stdin pattern (ARCHITECTURE.md §7).
        try:
            raw = sys.stdin.read(1024 * 1024 + 1)
        except OSError as exc:
            print(f"toolforge_install: batch stdin read: {exc}", file=sys.stderr)
            return 2
        if len(raw) > 1024 * 1024:
            print(
                "toolforge_install: batch stdin exceeded 1 MiB cap",
                file=sys.stderr,
            )
            return 2
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(
                f"toolforge_install: batch stdin not valid JSON: {exc}",
                file=sys.stderr,
            )
            return 2
        return install_batch(items, auto_yes=auto_yes)
    if len(rest) < 4:
        print(
            "Usage: toolforge_install.py <tool_name> <install_command> <category> [--yes]\n"
            "       toolforge_install.py --batch [--yes]  (JSON array on stdin)",
            file=sys.stderr,
        )
        return 2
    return install(rest[1], rest[2], rest[3], auto_yes=auto_yes)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
