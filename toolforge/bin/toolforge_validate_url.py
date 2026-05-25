#!/usr/bin/env python3
"""ToolForge URL allow-list gate. Hard security boundary for the curator skill.

Exit 0 + echo canonicalized hostname if host is in the allow-list.
Exit 1 + error to stderr otherwise.
Exit 0 + prints the allow-list when invoked with `--list` (used by the skill
to keep the WebFetch allowed_domains in sync with this single source of truth).
Exit 0/1 silently when invoked with `--check <url>` (for script callers).

IDN hosts are canonicalized to ASCII punycode before comparison to defend
against homograph attacks (e.g. Cyrillic 'і' visually equal to Latin 'i').
"""

from __future__ import annotations

import sys
from urllib.parse import urlparse

ALLOWED_HOSTS = frozenset({
    "github.com",
    "raw.githubusercontent.com",
    "claudemarketplaces.com",
    "modelcontextprotocol.io",
    "aitmpl.com",
    "npmjs.com",
    "www.npmjs.com",
})


def _canonicalize_host(host: str) -> str:
    if not host:
        return ""
    # Strip trailing dot (FQDN root): github.com. -> github.com
    if host.endswith("."):
        host = host[:-1]
    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as exc:
        print(f"toolforge url: IDN canonicalization failed for {host!r}: {exc}", file=sys.stderr)
        return "\x00invalid:" + host  # guaranteed allow-list miss; do NOT lower()


def _has_forbidden_bytes(url: str) -> bool:
    # Reject control bytes (\x00-\x1f) and backslash pre-parse.
    # urlparse silently tolerates these; some downstream HTTP clients normalize
    # backslash -> slash which enables parser-differential attacks.
    if "\\" in url:
        return True
    for ch in url:
        if ord(ch) < 0x20:
            return True
    return False


def is_allowed(url: str) -> bool:
    if _has_forbidden_bytes(url):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    raw_host = parsed.hostname or ""
    if not raw_host:
        print(f"toolforge url: missing hostname in {url!r}", file=sys.stderr)
        return False
    host = _canonicalize_host(raw_host)
    return host in ALLOWED_HOSTS


def _canonical_hostname(url: str) -> str:
    try:
        return _canonicalize_host(urlparse(url).hostname or "")
    except ValueError:
        return "<unparseable>"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: toolforge_validate_url.py <url|--list|--check <url>>", file=sys.stderr)
        return 2
    if argv[1] == "--list":
        for h in sorted(ALLOWED_HOSTS):
            print(h)
        return 0
    if argv[1] == "--check":
        if len(argv) < 3:
            return 1
        return 0 if is_allowed(argv[2]) else 1
    url = argv[1]
    if not is_allowed(url):
        host = _canonical_hostname(url) or "<unparseable>"
        print(f"toolforge url refused: {host} not in allow-list", file=sys.stderr)
        return 1
    print(_canonical_hostname(url))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
