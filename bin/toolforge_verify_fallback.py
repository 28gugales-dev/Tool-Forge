#!/usr/bin/env python3
"""Code-enforced integrity check for curator fallback JSON.

Replaces the LLM-prompt 'sha256sum -c' instruction in toolforge-curator/SKILL.md
(audit M2). An adversarial WebFetch result could prompt-inject around a
model-enforced verification step; a Python script with deterministic exit codes
cannot be argued out of.

Usage:
  python toolforge_verify_fallback.py <manifest>
  python toolforge_verify_fallback.py <file> <expected-hex|hash-file>

In manifest mode (one argument), parses a sha256sum-format file with one entry
per line: `<64 hex hash>  <relative path>`. Paths resolve against the manifest's
grandparent directory (so `toolforge/fallback/manifest.sha256` containing
`fallback/backend.json` is read as `toolforge/fallback/backend.json`). Each
referenced file is hashed and compared. Mismatches and missing files are
reported.

In direct-pair mode (two arguments), the second argument is either a literal
64-character hex digest or a path to a one-line hash file.

Exit codes:
  0  every checked file matched its expected digest ('OK' to stdout)
  1  usage error
  2  hash mismatch (stderr explains which file)
  3  missing file (manifest, referenced fallback file, or hash file)

Hash comparison uses secrets.compare_digest for constant-time equality.
"""

from __future__ import annotations

import hashlib
import secrets
import sys
from pathlib import Path

_HEX_LEN = 64


def _hash_file(path: Path) -> str:
    """sha256 of the LF-normalized bytes (CRLF -> LF, stray CR -> LF).

    Avoids hashlib.file_digest (3.11+) for 3.10 support. The fallback manifest is
    committed against git-canonical (LF) content; under core.autocrlf=true a
    Windows checkout has CRLF on disk and a byte-exact hash would spuriously
    mismatch. Normalizing matches git's text=auto digest (and toolforge_validate_repo
    ._sha256_lf) so the check passes identically on Windows and Linux.
    """
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _is_hex_digest(s: str) -> bool:
    return len(s) == _HEX_LEN and all(c in "0123456789abcdef" for c in s.lower())


def _resolve_inside(base: Path, rel: str) -> Path | None:
    """Resolve `rel` under `base`; reject if it escapes base via ../ traversal."""
    try:
        candidate = (base / rel).resolve()
        candidate.relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return candidate


def _check_pair(target: Path, expected_arg: str) -> int:
    if not target.is_file():
        print(f"toolforge: integrity check FAILED: {target} (file not found)", file=sys.stderr)
        return 3
    expected = expected_arg.strip().lower()
    if not _is_hex_digest(expected):
        hash_path = Path(expected_arg)
        if not hash_path.is_file():
            print(f"toolforge: integrity check FAILED: hash source {expected_arg} not found", file=sys.stderr)
            return 3
        # WARN: see SKETCHY_CODE_AUDIT.md#s4-11 — FIXED in F30 (empty hash file returns exit 3 instead of IndexError).
        tokens = hash_path.read_text(encoding="utf-8").strip().split()
        if not tokens:
            print(f"toolforge_verify_fallback: empty hash file {hash_path}", file=sys.stderr)
            return 3
        expected = tokens[0].lower()
        if not _is_hex_digest(expected):
            print(f"toolforge: integrity check FAILED: {hash_path} does not contain a sha256 hex digest", file=sys.stderr)
            return 3
    actual = _hash_file(target).lower()
    if not secrets.compare_digest(actual, expected):
        print(f"toolforge: integrity check FAILED: {target} expected={expected} actual={actual}", file=sys.stderr)
        return 2
    print("OK")
    return 0


def _check_manifest(manifest: Path) -> int:
    if not manifest.is_file():
        print(f"toolforge: integrity check FAILED: manifest {manifest} not found", file=sys.stderr)
        return 3
    base = manifest.resolve().parent.parent
    entries: list[tuple[str, str]] = []
    for lineno, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            print(f"toolforge: integrity check FAILED: {manifest}:{lineno} malformed line", file=sys.stderr)
            return 3
        digest, rel = parts[0].lower(), parts[1].lstrip("*").strip()
        if not _is_hex_digest(digest):
            print(f"toolforge: integrity check FAILED: {manifest}:{lineno} not a sha256 hex digest", file=sys.stderr)
            return 3
        entries.append((digest, rel))
    if not entries:
        print(f"toolforge: integrity check FAILED: {manifest} contains no entries", file=sys.stderr)
        return 3
    for expected, rel in entries:
        target = _resolve_inside(base, rel)
        if target is None:
            print(f"toolforge: integrity check FAILED: {rel} escapes manifest base dir {base}", file=sys.stderr)
            return 3
        if not target.is_file():
            print(f"toolforge: integrity check FAILED: {target} (file not found)", file=sys.stderr)
            return 3
        actual = _hash_file(target).lower()
        if not secrets.compare_digest(actual, expected):
            print(f"toolforge: integrity check FAILED: {target} expected={expected} actual={actual}", file=sys.stderr)
            return 2
    print("OK")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2:
        return _check_manifest(Path(argv[1]))
    if len(argv) == 3:
        return _check_pair(Path(argv[1]), argv[2])
    print(
        "Usage: toolforge_verify_fallback.py <manifest>\n"
        "       toolforge_verify_fallback.py <file> <expected-hex|hash-file>",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
