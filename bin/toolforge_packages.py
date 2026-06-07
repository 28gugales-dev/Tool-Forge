#!/usr/bin/env python3
"""ToolForge curated package manager. Loads, lists, and describes pre-built
tool bundles from catalog/packages/.

CLI output is JSON or formatted text. Exit 0 on success, 1 if not found,
2 on usage error, 3 on I/O error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

_PACKAGES_DIR = Path(__file__).parent.parent / "catalog" / "packages"
_MANIFEST = _PACKAGES_DIR / "manifest.json"


def _load_manifest() -> list[dict]:
    try:
        with _MANIFEST.open(encoding="utf-8") as f:
            return json.load(f).get("packages", [])
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        print(f"toolforge_packages: cannot read manifest: {exc}", file=sys.stderr)
        sys.exit(3)


def _load_package(filename: str) -> Optional[dict]:
    path = _PACKAGES_DIR / filename
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        print(f"toolforge_packages: cannot read {filename}: {exc}", file=sys.stderr)
        return None


def list_packages() -> list[dict]:
    """Summary listing of all available packages."""
    manifest = _load_manifest()
    summaries = []
    for entry in manifest:
        pkg = _load_package(entry["file"])
        if pkg:
            summaries.append({
                "id": pkg["package_id"],
                "display_name": pkg["display_name"],
                "tagline": pkg.get("tagline", ""),
                "tool_count": len(pkg.get("tools", [])),
                "requires_api_keys": pkg.get("requires_api_keys", []),
                "estimated_token_overhead": pkg.get("estimated_token_overhead", "unknown"),
                "best_for": pkg.get("best_for", []),
            })
    return summaries


def get_package(package_id: str) -> Optional[dict]:
    """Full package details including all tool install commands."""
    manifest = _load_manifest()
    for entry in manifest:
        if entry["id"] == package_id:
            return _load_package(entry["file"])
    return None


def render_package(pkg: dict) -> str:
    """Human-readable summary of a package for display in Claude context."""
    lines = [
        f"{'=' * 60}",
        f"Package: {pkg['display_name']}",
        f"  {pkg.get('tagline', '')}",
        f"{'=' * 60}",
        f"  {pkg.get('description', '')}",
        "",
        f"  Token overhead: {pkg.get('estimated_token_overhead', 'unknown')}",
    ]
    keys = pkg.get("requires_api_keys", [])
    if keys:
        lines.append(f"  API keys required: {', '.join(keys)}")
    else:
        lines.append("  API keys required: none")
    lines += ["", "  Tools:"]
    for tool in pkg.get("tools", []):
        lines.append(f"    [{tool['type'].upper()}] {tool['name']}")
        lines.append(f"      Why: {tool.get('why', '')}")
        lines.append(f"      Install: {tool.get('install_command', '')}")
        lines.append("")
    best_for = pkg.get("best_for", [])
    if best_for:
        lines.append(f"  Best for: {', '.join(best_for)}")
    token_note = pkg.get("token_savings_note")
    if token_note:
        lines.append(f"  Note: {token_note}")
    return "\n".join(lines)


def install_commands(package_id: str) -> list[str]:
    """Return just the ordered install commands for a package."""
    pkg = get_package(package_id)
    if not pkg:
        return []
    return [t["install_command"] for t in pkg.get("tools", []) if t.get("install_command")]


def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_packages.py list\n"
        "  toolforge_packages.py get <package_id>\n"
        "  toolforge_packages.py render <package_id>\n"
        "  toolforge_packages.py install_commands <package_id>\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "list":
        print(json.dumps(list_packages(), indent=2))
        return 0
    if cmd == "get":
        if len(argv) < 3:
            print("get requires a package_id", file=sys.stderr)
            return 2
        pkg = get_package(argv[2])
        if not pkg:
            print(f"package not found: {argv[2]}", file=sys.stderr)
            return 1
        print(json.dumps(pkg, indent=2))
        return 0
    if cmd == "render":
        if len(argv) < 3:
            print("render requires a package_id", file=sys.stderr)
            return 2
        pkg = get_package(argv[2])
        if not pkg:
            print(f"package not found: {argv[2]}", file=sys.stderr)
            return 1
        print(render_package(pkg))
        return 0
    if cmd == "install_commands":
        if len(argv) < 3:
            print("install_commands requires a package_id", file=sys.stderr)
            return 2
        cmds = install_commands(argv[2])
        if not cmds:
            print(f"package not found or empty: {argv[2]}", file=sys.stderr)
            return 1
        print(json.dumps(cmds))
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
