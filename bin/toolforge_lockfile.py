#!/usr/bin/env python3
"""Tamper-evident install lockfile: sha256-pins every file of an installed tool.

Closes the scan-once-then-trust hole: ToolForge's semantic malware scan runs
only at install time, so a skill modified afterwards (supply-chain drift,
malicious update, accidental edit) would be invisible forever. This module pins
per-file sha256 digests plus a sorted bundle hash at install time and
re-verifies them on demand (/toolforge-status Step 4).

Table ownership: this module owns the `install_artifacts` table and is the ONLY
reader/writer. It reuses toolforge_db._connect() for the shared SQLite handle
but deliberately stays OUTSIDE the PRAGMA user_version migration chain in
toolforge_db.init_db — safe because _ensure_table() is a single idempotent
CREATE TABLE IF NOT EXISTS under BEGIN IMMEDIATE, and no other module reads the
table, so there is no cross-module migration ordering to coordinate.

Bundle hash = sha256 over '\\n'.join(sorted(f"{rel}:{sha}")) — same shape as
autoskills' registry bundleHash, so a single value detects any add/remove/edit.

Usage:
  python toolforge_lockfile.py pin <tool_name> <dir>
  python toolforge_lockfile.py verify [<tool_name>|--all] [--json]
  python toolforge_lockfile.py unpin <tool_name>
  python toolforge_lockfile.py list
  python toolforge_lockfile.py --self-test

Exit codes (mirror toolforge_verify_fallback.py):
  0  every verified tool matched its pinned digests
  1  usage error / pin refused (caps, archives, bad args)
  2  any tool has changed or new files
  3  any tool has missing files (or its directory is gone)

Hash comparison uses secrets.compare_digest for constant-time equality.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import toolforge_db  # noqa: E402

# ---------- section: constants ----------

# Caps per toolforge_local_scan.py conventions: bound adversarial directory
# trees (hash-DoS via huge/many files) and refuse opaque archives outright.
SKIP_DIRS = {".git", "node_modules", "__pycache__"}
MAX_FILES = 500
MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MiB
ARCHIVE_EXTS = (
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".7z",
    ".rar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
)

# Tool install roots probed when verify has to re-derive a tool's directory.
_INSTALL_KINDS = ("skills", "plugins")


# ---------- section: hashing ----------


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_hash(entries: list[tuple[str, str]]) -> str:
    """entries: [(rel_path, sha256), ...] — order-independent bundle digest."""
    joined = "\n".join(sorted(f"{rel}:{sha}" for rel, sha in entries))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _walk_files(root: Path) -> list[str]:
    """Return sorted rel_paths (forward-slash) under root, enforcing caps.

    Raises ValueError on cap violation or archive file — callers either refuse
    the pin (exit 1) or report the tool as drifted.
    """
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    rels: list[str] = []
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fname in sorted(filenames):
            full = Path(dirpath) / fname
            if full.is_symlink():
                continue
            if fname.lower().endswith(ARCHIVE_EXTS):
                raise ValueError(f"refusing archive file: {full}")
            try:
                total_bytes += full.stat().st_size
            except OSError:
                continue
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError(
                    f"directory exceeds {MAX_TOTAL_BYTES} byte cap: {root}"
                )
            rels.append(full.relative_to(root).as_posix())
            if len(rels) > MAX_FILES:
                raise ValueError(f"directory exceeds {MAX_FILES} file cap: {root}")
    return rels


# ---------- section: db ----------


def _ensure_table(conn: sqlite3.Connection) -> None:
    # BEGIN IMMEDIATE: two concurrent pinners must not both race the CREATE.
    # Idempotent by IF NOT EXISTS; deliberately outside init_db's user_version
    # migration chain (see module docstring).
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS install_artifacts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name   TEXT NOT NULL,
                rel_path    TEXT NOT NULL,
                sha256      TEXT NOT NULL,
                bundle_hash TEXT NOT NULL,
                pinned_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(tool_name, rel_path)
            )
            """
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _open() -> sqlite3.Connection:
    conn = toolforge_db._connect()
    _ensure_table(conn)
    return conn


# ---------- section: pin / unpin ----------


def pin(tool_name: str, dir_path: str) -> int:
    """Hash every file under dir_path and pin it. Returns file count.

    DELETE existing rows + INSERT all new rows in one transaction so a crash
    mid-pin never leaves a half-old/half-new lockfile.
    """
    name = toolforge_db._validate_tool_name(tool_name)
    root = Path(dir_path).resolve()
    rels = _walk_files(root)
    entries = [(rel, _hash_file(root / Path(rel))) for rel in rels]
    bundle = _bundle_hash(entries)
    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM install_artifacts WHERE tool_name=?", (name,))
            conn.executemany(
                "INSERT INTO install_artifacts (tool_name, rel_path, sha256, bundle_hash) "
                "VALUES (?,?,?,?)",
                [(name, rel, sha, bundle) for rel, sha in entries],
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return len(entries)


def unpin(tool_name: str) -> int:
    """Remove all pinned rows for tool_name. Returns rows deleted."""
    name = toolforge_db._validate_tool_name(tool_name)
    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "DELETE FROM install_artifacts WHERE tool_name=?", (name,)
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return cur.rowcount
    finally:
        conn.close()


# ---------- section: verify ----------


def _pinned_tools() -> list[str]:
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT DISTINCT tool_name FROM install_artifacts ORDER BY tool_name"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _pinned_entries(tool_name: str) -> dict[str, str]:
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT rel_path, sha256 FROM install_artifacts WHERE tool_name=?",
            (tool_name,),
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}


def _tool_dir(tool_name: str) -> Optional[Path]:
    for kind in _INSTALL_KINDS:
        candidate = Path.home() / ".claude" / kind / tool_name
        if candidate.is_dir():
            return candidate
    return None


def verify_tool(tool_name: str, dir_path: Optional[str] = None) -> dict:
    """Rehash one tool against its pinned rows.

    Returns {"tool_name", "status": ok|modified|missing|unpinned,
             "pinned": n, "changed": [...], "missing": [...], "new": [...],
             "error": str|None}.
    """
    name = toolforge_db._validate_tool_name(tool_name)
    result: dict = {
        "tool_name": name,
        "status": "ok",
        "pinned": 0,
        "changed": [],
        "missing": [],
        "new": [],
        "error": None,
    }
    pinned = _pinned_entries(name)
    result["pinned"] = len(pinned)
    if not pinned:
        result["status"] = "unpinned"
        return result

    root = Path(dir_path).resolve() if dir_path else _tool_dir(name)
    if root is None or not root.is_dir():
        result["status"] = "missing"
        result["missing"] = sorted(pinned)
        result["error"] = "install directory not found"
        return result

    for rel, expected in sorted(pinned.items()):
        parts = rel.split("/")
        if (
            any(p in ("", ".", "..") for p in parts)
            or "\\" in rel
            or ":" in rel
        ):
            result["missing"].append(rel)
            continue
        target = root.joinpath(*parts)
        if not target.is_file():
            result["missing"].append(rel)
            continue
        actual = _hash_file(target).lower()
        if not secrets.compare_digest(actual, expected.lower()):
            result["changed"].append(rel)

    try:
        current = _walk_files(root)
        result["new"] = [rel for rel in current if rel not in pinned]
    except ValueError as exc:
        # Caps/archives violated post-install is itself drift worth flagging.
        result["error"] = str(exc)

    if result["missing"]:
        result["status"] = "missing"
    elif result["changed"] or result["new"] or result["error"]:
        result["status"] = "modified"
    return result


def _exit_for(results: list[dict]) -> int:
    if any(r["status"] == "missing" for r in results):
        return 3
    if any(r["status"] == "modified" for r in results):
        return 2
    return 0


def _render_result(r: dict) -> str:
    if r["status"] == "ok":
        return f"{r['tool_name']}: verified ({r['pinned']} files)"
    if r["status"] == "unpinned":
        return f"{r['tool_name']}: unpinned (no lockfile rows)"
    if r["status"] == "missing":
        rels = ", ".join(r["missing"][:5])
        return f"{r['tool_name']}: MISSING: {len(r['missing'])} file(s) ({rels})"
    drifted = len(r["changed"]) + len(r["new"])
    parts = [f"{r['tool_name']}: MODIFIED: {drifted} file(s) changed"]
    for rel in r["changed"]:
        parts.append(f"  changed: {rel}")
    for rel in r["new"]:
        parts.append(f"  new: {rel}")
    if r["error"]:
        parts.append(f"  error: {r['error']}")
    return "\n".join(parts)


def verify_cmd(tool_name: Optional[str], as_json: bool) -> int:
    names = [tool_name] if tool_name else _pinned_tools()
    results = [verify_tool(n) for n in names]
    if as_json:
        print(json.dumps({"results": results}, indent=2))
    else:
        if not results:
            print("No pinned tools. Install via /toolforge to start pinning.")
        for r in results:
            print(_render_result(r))
    return _exit_for(results)


# ---------- section: list ----------


def list_cmd() -> int:
    conn = _open()
    try:
        rows = conn.execute(
            "SELECT tool_name, COUNT(*), MIN(pinned_at), bundle_hash "
            "FROM install_artifacts GROUP BY tool_name ORDER BY tool_name"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("No pinned tools.")
        return 0
    for name, n, pinned_at, bundle in rows:
        print(f"{name}: {n} file(s) pinned at {pinned_at} bundle={bundle[:16]}...")
    return 0


# ---------- section: self-test ----------


def _self_test() -> int:
    """Pin/verify/drift/unpin roundtrip against a temp DB + fixture dir.

    Does NOT touch ~/.claude/toolforge.db.
    """
    saved = toolforge_db.DB_PATH
    tmpdir = Path(tempfile.mkdtemp(prefix="toolforge_lockfile_test_"))
    toolforge_db.DB_PATH = tmpdir / "toolforge.db"
    passed = 0
    failed = 0

    def check(desc: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            print(f"OK: {desc}")
            passed += 1
        else:
            print(f"FAIL: {desc}{(' — ' + detail) if detail else ''}")
            failed += 1

    try:
        fixture = tmpdir / "fixture-tool"
        (fixture / "sub").mkdir(parents=True)
        (fixture / "SKILL.md").write_text("# fixture\noriginal\n", encoding="utf-8")
        (fixture / "sub" / "b.txt").write_text("bbb\n", encoding="utf-8")

        n = pin("fixture-tool", str(fixture))
        check("pin counts 2 files", n == 2, f"got {n}")

        r = verify_tool("fixture-tool", str(fixture))
        check("verify ok after pin", r["status"] == "ok", json.dumps(r))
        check("exit 0 when all ok", _exit_for([r]) == 0)

        (fixture / "SKILL.md").write_text("# fixture\nTAMPERED\n", encoding="utf-8")
        r = verify_tool("fixture-tool", str(fixture))
        check(
            "edited file -> modified + rel_path reported",
            r["status"] == "modified" and r["changed"] == ["SKILL.md"],
            json.dumps(r),
        )
        check("exit 2 on modified", _exit_for([r]) == 2)

        (fixture / "sub" / "b.txt").unlink()
        r = verify_tool("fixture-tool", str(fixture))
        check(
            "deleted file -> missing + rel_path reported",
            r["status"] == "missing" and "sub/b.txt" in r["missing"],
            json.dumps(r),
        )
        check("exit 3 on missing (beats modified)", _exit_for([r]) == 3)

        (fixture / "extra.py").write_text("print('new')\n", encoding="utf-8")
        n = pin("fixture-tool", str(fixture))
        check("re-pin picks up current state", n == 2, f"got {n}")
        (fixture / "another.md").write_text("drift\n", encoding="utf-8")
        r = verify_tool("fixture-tool", str(fixture))
        check(
            "new unpinned file -> modified + listed as new",
            r["status"] == "modified" and r["new"] == ["another.md"],
            json.dumps(r),
        )

        deleted = unpin("fixture-tool")
        check("unpin removes rows", deleted == 2, f"got {deleted}")
        r = verify_tool("fixture-tool", str(fixture))
        check("verify after unpin -> unpinned", r["status"] == "unpinned")
        check("list empty after unpin", _pinned_tools() == [])

        evil = tmpdir / "evil-tool"
        evil.mkdir()
        (evil / "payload.zip").write_bytes(b"PK\x03\x04junk")
        try:
            pin("evil-tool", str(evil))
            check("archive extension refused at pin", False, "pin accepted .zip")
        except ValueError as exc:
            check("archive extension refused at pin", "archive" in str(exc))
    finally:
        toolforge_db.DB_PATH = saved
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- section: cli-dispatcher ----------

_USAGE = (
    "Usage: toolforge_lockfile.py pin <tool_name> <dir>\n"
    "       toolforge_lockfile.py verify [<tool_name>|--all] [--json]\n"
    "       toolforge_lockfile.py unpin <tool_name>\n"
    "       toolforge_lockfile.py list\n"
    "       toolforge_lockfile.py --self-test"
)


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return _self_test()
    if len(argv) < 2:
        print(_USAGE, file=sys.stderr)
        return 1
    cmd = argv[1]
    try:
        if cmd == "pin":
            if len(argv) != 4:
                print(_USAGE, file=sys.stderr)
                return 1
            n = pin(argv[2], argv[3])
            print(f"Pinned {n} file(s) for {argv[2]}.")
            return 0
        if cmd == "verify":
            as_json = "--json" in argv
            rest = [a for a in argv[2:] if a not in {"--json", "--all"}]
            if len(rest) > 1:
                print(_USAGE, file=sys.stderr)
                return 1
            return verify_cmd(rest[0] if rest else None, as_json)
        if cmd == "unpin":
            if len(argv) != 3:
                print(_USAGE, file=sys.stderr)
                return 1
            n = unpin(argv[2])
            print(f"Unpinned {n} row(s) for {argv[2]}.")
            return 0
        if cmd == "list":
            return list_cmd()
    except ValueError as exc:
        print(f"toolforge_lockfile refused: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"toolforge_lockfile: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(_USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
