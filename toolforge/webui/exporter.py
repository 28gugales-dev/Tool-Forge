"""Turn a saved flow into an installed Claude skill.

Output: ~/.claude/skills/toolforge-<slug>/SKILL.md with a frontmatter description that
contains MANDATORY trigger phrases (per skill-creator guidance: skills undertrigger)
and a numbered chain body Claude can walk top-to-bottom.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))
USER_SKILLS_DIR = HOME / ".claude" / "skills"
SLUG_RE = re.compile(r"[^a-z0-9-]+")

_EXPORT_LOCKS: dict[str, threading.Lock] = {}
_EXPORT_LOCKS_MUTEX = threading.Lock()


def _trigger_lock(trigger: str) -> threading.Lock:
    with _EXPORT_LOCKS_MUTEX:
        return _EXPORT_LOCKS.setdefault(trigger, threading.Lock())


# ---------- section: helpers ----------


def slugify(name: str) -> str:
    s = SLUG_RE.sub("-", name.lower()).strip("-")
    return s or "untitled-flow"


# ---------- section: cycle-detection ----------


class FlowCycleError(ValueError):
    """Raised when the flow graph contains a cycle. Carries the offending node-id ring."""

    def __init__(self, cycle: list[str]):
        super().__init__(f"flow contains cycle: {' -> '.join(cycle)}")
        self.cycle = cycle


def detect_cycles(nodes: list[dict], edges: list[dict]) -> list[list[str]]:
    """Return list of cycles (each a list of node-ids forming a ring). Empty = DAG.

    Uses iterative DFS with three-color marking (white/gray/black). When a gray
    node is re-encountered, the slice of the stack from that node onward is the
    cycle. Order is deterministic given input order.
    """
    ids = [str(n["id"]) for n in nodes]
    graph: dict[str, list[str]] = defaultdict(list)
    id_set = set(ids)
    for e in edges:
        a, b = str(e["source"]), str(e["target"])
        if a in id_set and b in id_set:
            graph[a].append(b)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {nid: WHITE for nid in ids}
    cycles: list[list[str]] = []
    seen_cycle_keys: set[tuple[str, ...]] = set()

    for start in ids:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        path_pos: dict[str, int] = {}
        while stack:
            nid, idx = stack[-1]
            if idx == 0:
                color[nid] = GRAY
                path_pos[nid] = len(path)
                path.append(nid)
            neighbors = graph.get(nid, [])
            if idx < len(neighbors):
                stack[-1] = (nid, idx + 1)
                nxt = neighbors[idx]
                if color[nxt] == GRAY:
                    ring = path[path_pos[nxt]:] + [nxt]
                    key = tuple(sorted(ring))
                    if key not in seen_cycle_keys:
                        seen_cycle_keys.add(key)
                        cycles.append(ring)
                elif color[nxt] == WHITE:
                    stack.append((nxt, 0))
            else:
                color[nid] = BLACK
                path.pop()
                path_pos.pop(nid, None)
                stack.pop()
    return cycles


def _topo_order(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Topological sort of nodes by edges. Raises FlowCycleError on cycle.

    No silent leftover-append: a graph with a cycle has no valid linear chain,
    so the exporter MUST refuse rather than emit a SKILL.md whose step order
    is meaningless inside the ring.
    """
    if not edges:
        return sorted(nodes, key=lambda n: (n.get("pos_x", 0), n.get("pos_y", 0)))
    cycles = detect_cycles(nodes, edges)
    if cycles:
        raise FlowCycleError(cycles[0])
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
        for nxt in sorted(graph[nid],
                          key=lambda x: (by_id[x].get("pos_x", 0), by_id[x].get("pos_y", 0))):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    return order


# ---------- section: skill-md-rendering ----------


def render_skill_md(flow: dict) -> dict:
    """Render the flow as SKILL.md text WITHOUT touching disk. Returns
    {ok, markdown, steps, trigger, name, warnings} or raises FlowCycleError.

    Powers both the live preview endpoint and the actual exporter; they share
    one source of truth so what the user sees in the preview pane is byte-for-
    byte what gets written to ~/.claude/skills/.
    """
    name = flow.get("name") or "Untitled Flow"
    trigger = (flow.get("trigger") or slugify(name)).lstrip("/")
    description = flow.get("description") or f"ToolForge-authored flow: {name}"
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    if not nodes:
        raise ValueError("flow has no nodes")

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

    trigger_slug = slugify(trigger)
    if trigger_slug == "untitled-flow" or not trigger_slug.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"Trigger {trigger!r} must contain at least one alphanumeric character.")
    skill_slug = f"toolforge-{trigger_slug}"
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

    return {
        "ok": True,
        "markdown": "\n".join(lines),
        "steps": len(ordered),
        "trigger": trigger,
        "name": name,
        "skill_slug": skill_slug,
    }


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


# ---------- section: disk-io ----------


# WARN: see SKETCHY_CODE_AUDIT.md#s2-2 — FIXED in F10 (atomic write via <skill_dir>.tmp + os.replace; flow.json written before SKILL.md so a partial write leaves the failure-marker file, not an orphan skill).
def export_flow(flow: dict, target_dir: Path | None = None) -> dict:
    """Render flow + persist SKILL.md and flow.json.

    flow shape:
    {
      "name": "Landing Page",
      "trigger": "toolforge-landingpage",
      "description": "Generate polished landing page via taste-skill -> impeccable -> design-review",
      "nodes": [{id, tool:{type,name,invoke,path}, annotation, pos_x, pos_y}, ...],
      "edges": [{source, target}, ...]
    }

    Raises FlowCycleError when graph is cyclic (caller must surface to UI).
    """
    rendered = render_skill_md(flow)
    target_dir = target_dir or USER_SKILLS_DIR
    # WARN: see SKETCHY_CODE_AUDIT.md#s4-6 — FIXED in F29 (render_skill_md now raises ValueError when slugify(trigger) collapses to "untitled-flow"; export_flow inherits the guard since it always routes through render_skill_md).
    skill_dir = target_dir / rendered["skill_slug"]
    # WARN: see SKETCHY_CODE_AUDIT.md#s4-5 — FIXED in F28 (per-trigger threading.Lock serializes concurrent /api/export of same trigger; ThreadingHTTPServer is single-process so threading.Lock is sufficient).
    # Path(str(...) + ".tmp") rather than with_suffix: skill_dir has no suffix, so with_suffix would still work but the explicit form is clearer.
    tmp_dir = Path(str(skill_dir) + ".tmp")
    with _trigger_lock(rendered["trigger"]):
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
            tmp_dir.mkdir(parents=True)
            # flow.json first: if SKILL.md write fails, the leftover temp dir still has the failure-marker file.
            (tmp_dir / "flow.json").write_text(json.dumps(flow, indent=2), encoding="utf-8")
            (tmp_dir / "SKILL.md").write_text(rendered["markdown"], encoding="utf-8")
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
            os.replace(tmp_dir, skill_dir)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
    skill_md_path = skill_dir / "SKILL.md"
    return {
        "ok": True,
        "skill_slug": rendered["skill_slug"],
        "skill_path": str(skill_md_path),
        "trigger": rendered["trigger"],
        "steps": rendered["steps"],
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


def delete_exported_flow(
    trigger: str, target_dir: Path | None = None
) -> tuple[bool, list[str]]:
    """Delete an exported skill directory.

    Returns ``(success, errors)`` where ``success`` is ``True`` iff every
    filesystem operation succeeded (or the directory was already absent).
    ``errors`` contains one ``"<path>: <exception>"`` string per failure
    (permission denied, AV lock, ENOTEMPTY, etc.). The function never raises
    OSError -- failures are reported via the return value so the caller can
    surface them to the UI instead of lying about success.
    """
    target_dir = target_dir or USER_SKILLS_DIR
    trigger_slug = slugify(trigger)
    # WARN: see SKETCHY_CODE_AUDIT.md#s4-6 — FIXED in F29 (reject blank/symbol-only trigger so delete cannot target the "untitled-flow" collision bucket).
    if trigger_slug == "untitled-flow" or not trigger_slug.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"Trigger {trigger!r} must contain at least one alphanumeric character.")
    skill_dir = target_dir / f"toolforge-{trigger_slug}"
    if not skill_dir.exists():
        return True, []
    errors: list[str] = []
    for p in sorted(skill_dir.rglob("*"), reverse=True):
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
            else:
                p.rmdir()
        except OSError as exc:
            errors.append(f"{p}: {exc}")
    try:
        skill_dir.rmdir()
    except OSError as exc:
        errors.append(f"{skill_dir}: {exc}")
    return len(errors) == 0, errors


if __name__ == "__main__":
    import sys
    flow = json.load(sys.stdin)
    print(json.dumps(export_flow(flow), indent=2))
