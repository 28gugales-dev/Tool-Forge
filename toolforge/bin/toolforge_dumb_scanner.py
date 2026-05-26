#!/usr/bin/env python3
"""Day-1 dumb scanner. Counts tool_use invocations across all session transcripts.

Validates the router thesis: are skills actually getting ignored?

Usage: python toolforge_dumb_scanner.py [--days 30]
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

DEFAULT_DAYS = 30
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")


def parse_ts(ts: str) -> float:
    """Parse ISO 8601 to epoch seconds. Returns 0 on failure."""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def scan(days: int) -> dict:
    cutoff = time.time() - days * 86400
    tool_counts: Counter = Counter()
    skill_counts: Counter = Counter()
    mcp_counts: Counter = Counter()
    agent_counts: Counter = Counter()
    transcripts_scanned = 0
    events_scanned = 0
    tool_uses_found = 0

    for jsonl_path in glob.iglob(PROJECTS_GLOB):
        try:
            if os.stat(jsonl_path).st_mtime < cutoff:
                continue
        except OSError:
            continue
        transcripts_scanned += 1
        try:
            with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    events_scanned += 1
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") != "assistant":
                        continue
                    ts_epoch = parse_ts(evt.get("timestamp", ""))
                    if ts_epoch and ts_epoch < cutoff:
                        continue
                    msg = evt.get("message", {})
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        tool_uses_found += 1
                        name = block.get("name", "")
                        tool_counts[name] += 1
                        if name == "Skill":
                            skill = (block.get("input") or {}).get("skill", "<unknown>")
                            skill_counts[skill] += 1
                        elif name == "Task":
                            subtype = (block.get("input") or {}).get("subagent_type", "<unknown>")
                            agent_counts[subtype] += 1
                        elif name.startswith("mcp__"):
                            parts = name.split("__")
                            server = parts[1] if len(parts) > 1 else "<unknown>"
                            mcp_counts[server] += 1
        except OSError:
            continue

    return {
        "window_days": days,
        "transcripts_scanned": transcripts_scanned,
        "events_scanned": events_scanned,
        "tool_uses_found": tool_uses_found,
        "tool_counts": dict(tool_counts.most_common()),
        "skill_counts": dict(skill_counts.most_common()),
        "mcp_counts": dict(mcp_counts.most_common()),
        "agent_counts": dict(agent_counts.most_common()),
    }


def main(argv: list[str]) -> int:
    days = DEFAULT_DAYS
    for i, a in enumerate(argv):
        if a == "--days" and i + 1 < len(argv):
            try:
                days = int(argv[i + 1])
            except ValueError:
                pass
    result = scan(days)
    print(f"\n=== ToolForge Dumb Scanner — last {result['window_days']} days ===")
    print(f"Transcripts scanned:  {result['transcripts_scanned']}")
    print(f"Events scanned:       {result['events_scanned']}")
    print(f"tool_use blocks:      {result['tool_uses_found']}")
    print(f"\n--- Top 20 raw tool invocations ---")
    for n, (k, v) in enumerate(list(result["tool_counts"].items())[:20], 1):
        print(f"  {n:2}. {k:30}  {v:>6}")
    print(f"\n--- Skill invocations (via Skill tool) ---")
    if result["skill_counts"]:
        for n, (k, v) in enumerate(result["skill_counts"].items(), 1):
            print(f"  {n:2}. {k:50}  {v:>6}")
    else:
        print("  (none — Skill tool never fired in window)")
    print(f"\n--- MCP servers used ---")
    if result["mcp_counts"]:
        for n, (k, v) in enumerate(result["mcp_counts"].items(), 1):
            print(f"  {n:2}. {k:30}  {v:>6}")
    else:
        print("  (none)")
    print(f"\n--- Subagents spawned (Task tool) ---")
    if result["agent_counts"]:
        for n, (k, v) in enumerate(result["agent_counts"].items(), 1):
            print(f"  {n:2}. {k:40}  {v:>6}")
    else:
        print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
