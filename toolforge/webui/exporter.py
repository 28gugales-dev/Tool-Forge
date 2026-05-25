"""Turn a saved flow into an installed Claude skill.

Output: ~/.claude/skills/toolforge-<slug>/SKILL.md with a frontmatter description that
contains MANDATORY trigger phrases (per skill-creator guidance: skills undertrigger)
and a numbered chain body Claude can walk top-to-bottom.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))
USER_SKILLS_DIR = HOME / ".claude" / "skills"
SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(name: str) -> str:
    s = SLUG_RE.sub("-", name.lower()).strip("-")
    return s or "untitled-flow"


def _topo_order(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Topological sort of nodes by edges. Falls back to position-based order."""
    if not edges:
        return sorted(nodes, key=lambda n: (n.get("pos_x", 0), n.get("pos_y", 0)))
    indeg: dict[str, int] = defaultdict(int)
    graph: dict[str, list[str]] = defaultdict(list)
    ids = {str(n["id"]) for n in nodes}
    for e in edges:
        a, b = str(e["source"]), str(e["target"])
        if a in ids and b in ids:
            graph[a].append(b)
            indeg[b] += 1
    by_id = {str(n["id"]): n for n in nodes}
    queue = deque(sorted([nid for nid in ids if indeg[nid] == 0],
                         key=lambda nid: (by_id[nid].get("pos_x", 0), by_id[nid].get("pos_y", 0))))
    order: list[dict] = []
    while queue:
        nid = queue.popleft()
        order.append(by_id[nid])
        for nxt in graph[nid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(nodes):
        leftover = [by_id[nid] for nid in ids if nid not in {str(n["id"]) for n in order}]
        leftover.sort(key=lambda n: (n.get("pos_x", 0), n.get("pos_y", 0)))
        order.extend(leftover)
    return order


def _invoke_instruction(tool: dict) -> str:
    t = tool.get("type", "skill")
    name = tool.get("name", "")
    invoke = tool.get("invoke") or name
    if t == "skill":
        return f"Invoke the Skill tool with `skill: \"{name}\"`."
    if t == "command":
        return f"Run the slash command `{name}` (or invoke Skill if registered as one)."
    if t == "mcp":
        return f"Use MCP server `{name}` (tool names prefixed with `{invoke}_`)."
    if t == "plugin":
        return f"Use a tool/skill/agent from plugin `{name}`."
    if t == "agent":
        return f"Spawn an Agent with subagent_type `{name}` (or load `{tool.get('path','')}`)."
    if t == "repo":
        return f"Reference repo `{name}` at `{tool.get('path','')}` for patterns/agents/skills."
    return f"Use `{name}`."


def export_flow(flow: dict, target_dir: Path | None = None) -> dict:
    """flow shape:
    {
      "name": "Landing Page",
      "trigger": "toolforge-landingpage",
      "description": "Generate polished landing page via taste-skill -> impeccable -> design-review",
      "nodes": [{id, tool:{type,name,invoke,path}, annotation, pos_x, pos_y}, ...],
      "edges": [{source, target}, ...]
    }
    """
    name = flow.get("name") or "Untitled Flow"
    trigger = (flow.get("trigger") or slugify(name)).lstrip("/")
    description = flow.get("description") or f"ToolForge-authored flow: {name}"
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []

    if not nodes:
        raise ValueError("flow has no nodes")

    target_dir = target_dir or USER_SKILLS_DIR
    skill_slug = f"toolforge-{slugify(trigger)}"
    skill_dir = target_dir / skill_slug
    skill_dir.mkdir(parents=True, exist_ok=True)

    ordered = _topo_order(nodes, edges)

    trigger_phrases = [
        f"/{trigger}", trigger, name.lower(),
        f"run the {name.lower()} flow", f"toolforge {name.lower()}",
    ]
    triggers_csv = ", ".join(sorted(set(trigger_phrases)))

    yaml_desc = (
        f"MANDATORY: invoke whenever the user types {triggers_csv} or asks to "
        f"\"{name.lower()}\" using the ToolForge-authored chain. "
        f"{description.strip()}"
    )

    lines: list[str] = ["---", f"name: {skill_slug}",
                        f"description: {yaml_desc}", "metadata:",
                        "  source: toolforge-webui",
                        f"  trigger: {trigger}",
                        "---", "",
                        f"# {name}", "",
                        f"**Trigger phrases:** {triggers_csv}", "",
                        f"{description}", "",
                        "## Chain (walk top-to-bottom, stop only if a step fails)", ""]
    for i, node in enumerate(ordered, start=1):
        tool = node.get("tool") or {}
        anno = (node.get("annotation") or "").strip() or "(no extra prompt)"
        lines.append(f"### Step {i}: {tool.get('name','(unnamed)')} [{tool.get('type','tool')}]")
        lines.append("")
        lines.append(f"- **How to invoke:** {_invoke_instruction(tool)}")
        lines.append(f"- **Source:** `{tool.get('source','')}` @ `{tool.get('path','')}`")
        lines.append(f"- **Prompt / annotation for this step:**")
        for ln in anno.splitlines() or [anno]:
            lines.append(f"  > {ln}")
        lines.append("")

    lines += ["## Completion criteria", "",
              "- Each step's tool ran without error.",
              "- Final step's deliverable matches the user's original ask.",
              "- Report a one-line summary plus a `**Used:**` line listing every step.", ""]

    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text("\n".join(lines), encoding="utf-8")

    (skill_dir / "flow.json").write_text(json.dumps(flow, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "skill_slug": skill_slug,
        "skill_path": str(skill_md_path),
        "trigger": trigger,
        "steps": len(ordered),
    }


def list_exported_flows(target_dir: Path | None = None) -> list[dict]:
    target_dir = target_dir or USER_SKILLS_DIR
    if not target_dir.exists():
        return []
    out: list[dict] = []
    for sub in target_dir.glob("toolforge-*"):
        flow_json = sub / "flow.json"
        if flow_json.exists():
            try:
                out.append(json.loads(flow_json.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def delete_exported_flow(trigger: str, target_dir: Path | None = None) -> bool:
    target_dir = target_dir or USER_SKILLS_DIR
    skill_dir = target_dir / f"toolforge-{slugify(trigger)}"
    if not skill_dir.exists():
        return False
    for p in sorted(skill_dir.rglob("*"), reverse=True):
        try:
            p.unlink() if p.is_file() else p.rmdir()
        except OSError:
            pass
    try:
        skill_dir.rmdir()
    except OSError:
        pass
    return True


if __name__ == "__main__":
    import sys
    flow = json.load(sys.stdin)
    print(json.dumps(export_flow(flow), indent=2))
