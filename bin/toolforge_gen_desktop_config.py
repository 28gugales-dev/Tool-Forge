#!/usr/bin/env python3
"""Generate a claude_desktop_config.json fragment for one or more catalog tools.

Usage:
  python bin/toolforge_gen_desktop_config.py <tool1> [tool2 ...]
  python bin/toolforge_gen_desktop_config.py --package best-for-coding
  python bin/toolforge_gen_desktop_config.py --list
  python bin/toolforge_gen_desktop_config.py --all

Output is a JSON object suitable for merging into:
  macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json
  Windows: %APPDATA%\\Claude\\claude_desktop_config.json

Example:
  python bin/toolforge_gen_desktop_config.py sequential-thinking memory context7
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG_FILE = HERE.parent / "catalog" / "catalog.json"
PACKAGES_DIR = HERE.parent / "catalog" / "packages"


def load_catalog() -> dict[str, dict]:
    with open(CATALOG_FILE, encoding="utf-8") as f:
        entries = json.load(f)
    return {e["name"]: e for e in entries}


def load_package(package_id: str) -> list[str]:
    pkg_file = PACKAGES_DIR / f"{package_id}.json"
    if not pkg_file.exists():
        available = [p.stem for p in PACKAGES_DIR.glob("*.json")]
        print(f"Package '{package_id}' not found. Available: {', '.join(sorted(available))}", file=sys.stderr)
        sys.exit(1)
    with open(pkg_file, encoding="utf-8") as f:
        pkg = json.load(f)
    return [t["name"] for t in pkg.get("tools", [])]


def build_fragment(names: list[str], catalog: dict[str, dict]) -> dict:
    servers: dict[str, dict] = {}
    warnings: list[str] = []

    for name in names:
        entry = catalog.get(name)
        if not entry:
            warnings.append(f"Warning: '{name}' not found in catalog — skipped.")
            continue

        cfg = entry.get("desktop_config")
        if not cfg:
            warnings.append(f"Warning: '{name}' has no desktop_config — skipped.")
            continue

        key = name
        servers[key] = {k: v for k, v in cfg.items()}

        if entry.get("requires_api_key") and "env" in cfg:
            env_var = entry.get("api_key_env", "")
            api_url = entry.get("api_key_url", "")
            hint = f"  {name}: set {env_var}"
            if api_url:
                hint += f" — get key at {api_url}"
            warnings.append(hint)

        if entry.get("requires_config") and entry.get("config_note"):
            warnings.append(f"  {name}: {entry['config_note']}")

    return {"mcpServers": servers}, warnings


def list_catalog(catalog: dict[str, dict]) -> None:
    print("Available tools in catalog:\n")
    for name, entry in catalog.items():
        key_note = " [API key required]" if entry.get("requires_api_key") else ""
        print(f"  {name:<25} {entry['display_name']}{key_note}")


def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    catalog = load_catalog()

    if args[0] == "--list":
        list_catalog(catalog)
        sys.exit(0)

    if args[0] == "--all":
        names = list(catalog.keys())
    elif args[0] == "--package":
        if len(args) < 2:
            print("Usage: --package <package-id>", file=sys.stderr)
            sys.exit(1)
        names = load_package(args[1])
    else:
        names = args

    fragment, warnings = build_fragment(names, catalog)

    print(json.dumps(fragment, indent=2))

    if warnings:
        print("\n# Notes:", file=sys.stderr)
        for w in warnings:
            print(w, file=sys.stderr)
        print(
            "\nMerge the above JSON into your Claude Desktop config file:\n"
            "  macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json\n"
            "  Windows: %APPDATA%\\Claude\\claude_desktop_config.json\n"
            "Then restart Claude Desktop.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
