#!/usr/bin/env python3
"""ToolForge organisation management. Handles org profiles and shared skill
stacks so teams can share a curated tool library and admins can push updates
to all members.

Org data lives in the shared SQLite DB alongside personal data. An org_id
acts as a namespace. Members who set the same org_id in toolforge-config.json
share stacks.

CLI exit codes: 0 success, 1 not found, 2 usage error, 3 I/O error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import toolforge_db as db

_CONFIG_PATH = Path(os.path.expanduser("~/.claude/toolforge-config.json"))


# ---------- config helpers ----------

def _read_config() -> dict:
    try:
        if _CONFIG_PATH.exists():
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_current_org_id() -> Optional[str]:
    return _read_config().get("org_id")


def set_current_org_id(org_id: str) -> None:
    cfg = _read_config()
    cfg["org_id"] = org_id
    _write_config(cfg)


def clear_current_org_id() -> None:
    cfg = _read_config()
    cfg.pop("org_id", None)
    _write_config(cfg)


# ---------- org operations ----------

def create_org(org_id: str, org_name: str, admin_email: Optional[str] = None,
               shared_catalog: bool = True) -> dict:
    db.save_org_profile(org_id, org_name, admin_email, shared_catalog)
    return db.get_org_profile(org_id)


def get_org(org_id: str) -> Optional[dict]:
    return db.get_org_profile(org_id)


def list_orgs() -> list[dict]:
    return db.list_org_profiles()


# ---------- stack operations ----------

def push_stack(stack_name: str, display_name: str, description: str,
               skills: list[str], org_id: Optional[str] = None,
               is_builtin: bool = False) -> None:
    db.save_skill_stack(stack_name, display_name, description, skills, org_id, is_builtin)


def get_stack(stack_name: str) -> Optional[dict]:
    return db.get_skill_stack(stack_name)


def list_stacks(org_id: Optional[str] = None) -> list[dict]:
    return db.list_skill_stacks(org_id)


def delete_stack(stack_name: str) -> bool:
    return db.delete_skill_stack(stack_name)


def render_stack(stack: dict) -> str:
    org_label = f" (org: {stack['org_id']})" if stack.get("org_id") else " (personal)"
    builtin_label = " [built-in]" if stack.get("is_builtin") else ""
    lines = [
        f"Stack: {stack['display_name']}{builtin_label}{org_label}",
        f"  ID: {stack['stack_name']}",
        f"  {stack.get('description', '')}",
        "",
        f"  Skills ({len(stack['skills'])}):",
    ]
    for skill in stack["skills"]:
        lines.append(f"    - {skill}")
    return "\n".join(lines)


def render_org_dashboard(org_id: str) -> str:
    org = get_org(org_id)
    if not org:
        return f"No organisation found with id: {org_id}"
    stacks = list_stacks(org_id)
    lines = [
        "=" * 55,
        f"Organisation: {org['org_name']}",
        f"  ID:          {org['org_id']}",
        f"  Admin:       {org.get('admin_email') or 'not set'}",
        f"  Shared catalog: {'yes' if org['shared_catalog'] else 'no'}",
        f"  Created:     {org.get('created_at', 'unknown')}",
        "",
        f"  Skill stacks ({len(stacks)}):",
    ]
    if not stacks:
        lines.append("    (none — use /toolforge-admin to create stacks)")
    else:
        for s in stacks:
            builtin = " [built-in]" if s.get("is_builtin") else ""
            lines.append(f"    {s['stack_name']:<30} {len(s['skills'])} skills{builtin}")
    lines.append("=" * 55)
    return "\n".join(lines)


# ---------- CLI ----------

def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_org.py create <org_id> <org_name> [<admin_email>]\n"
        "  toolforge_org.py get <org_id>\n"
        "  toolforge_org.py list\n"
        "  toolforge_org.py dashboard <org_id>\n"
        "  toolforge_org.py set_current <org_id>\n"
        "  toolforge_org.py get_current\n"
        "  toolforge_org.py clear_current\n"
        "  toolforge_org.py push_stack <stack_name> <display_name> <description> <skills_json> [<org_id>]\n"
        "  toolforge_org.py get_stack <stack_name>\n"
        "  toolforge_org.py list_stacks [<org_id>]\n"
        "  toolforge_org.py delete_stack <stack_name>\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    cmd = argv[1]
    try:
        if cmd == "create":
            email = argv[4] if len(argv) > 4 else None
            org = create_org(argv[2], argv[3], email)
            print(json.dumps(org, indent=2))
            return 0
        if cmd == "get":
            org = get_org(argv[2])
            print(json.dumps(org))
            return 0 if org else 1
        if cmd == "list":
            print(json.dumps(list_orgs(), indent=2))
            return 0
        if cmd == "dashboard":
            print(render_org_dashboard(argv[2]))
            return 0
        if cmd == "set_current":
            set_current_org_id(argv[2])
            print(f"org_id set to {argv[2]!r}")
            return 0
        if cmd == "get_current":
            oid = get_current_org_id()
            print(oid or "(none)")
            return 0
        if cmd == "clear_current":
            clear_current_org_id()
            print("org_id cleared")
            return 0
        if cmd == "push_stack":
            org = argv[6] if len(argv) > 6 else None
            skills = json.loads(argv[5])
            push_stack(argv[2], argv[3], argv[4], skills, org)
            print("ok")
            return 0
        if cmd == "get_stack":
            stack = get_stack(argv[2])
            print(json.dumps(stack))
            return 0 if stack else 1
        if cmd == "list_stacks":
            org = argv[2] if len(argv) > 2 else get_current_org_id()
            print(json.dumps(list_stacks(org), indent=2))
            return 0
        if cmd == "delete_stack":
            ok = delete_stack(argv[2])
            print("ok" if ok else "not found (or built-in)")
            return 0
    except (IndexError, ValueError) as exc:
        print(f"toolforge_org: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"toolforge_org: {exc}", file=sys.stderr)
        return 3
    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
