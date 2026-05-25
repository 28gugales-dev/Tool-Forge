"""Scan local Claude install for skills, plugins, MCPs, agents, commands, discovery repos.

Joins each item with the toolforge.db Bayesian-shrunk rating so the UI can rank inline.
Pure stdlib. Returns JSON-serializable list. Failures degrade silent (missing dir => skip).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

try:
    import toolforge_db  # type: ignore
except Exception:  # noqa: BLE001
    toolforge_db = None  # graceful

HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = HOME / ".claude"
PLUGINS_DIR = CLAUDE_DIR / "plugins"
MARKETPLACES_DIR = PLUGINS_DIR / "marketplaces"
USER_SKILLS_DIR = CLAUDE_DIR / "skills"
USER_COMMANDS_DIR = CLAUDE_DIR / "commands"
USER_AGENTS_DIR = CLAUDE_DIR / "agents"
PLUGIN_CACHE = PLUGINS_DIR / "cache"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)

DISCOVERY_REPOS = [
    ("everything-claude-code", "C:/Users/soham/everything-claude-code", "comprehensive harness"),
    ("wshobson-agents", "C:/Users/soham/wshobson-agents", "100+ domain agents"),
    ("agents-claude-code", "C:/Users/soham/agents-claude-code", "104 role agents"),
    ("awesome-claude-agents", "C:/Users/soham/awesome-claude-agents", "core+orchestrator agents"),
    ("claude-skills", "C:/Users/soham/claude-skills", "domain skills"),
    ("antigravity-awesome-skills", "C:/Users/soham/antigravity-awesome-skills", "writing/humanizer"),
    ("ruflo", "C:/Users/soham/ruflo", "multi-agent swarms"),
    ("awesome-claude-code", "C:/Users/soham/awesome-claude-code", "canonical discovery index"),
    ("andrej-karpathy-skills", "C:/Users/soham/andrej-karpathy-skills", "CLAUDE.md template"),
    ("claude-mem", "C:/Users/soham/claude-mem", "persistent cross-session memory"),
    ("alirezarezvani-claude-skills", "C:/Users/soham/alirezarezvani-claude-skills", "245+ multi-domain"),
    ("composio-awesome-claude-plugins", "C:/Users/soham/composio-awesome-claude-plugins", "Composio plugins"),
    ("composio-awesome-claude-skills", "C:/Users/soham/composio-awesome-claude-skills", "Composio skills"),
    ("travisvn-awesome-claude-skills", "C:/Users/soham/travisvn-awesome-claude-skills", "alt curation"),
    ("quemsah-awesome-claude-plugins", "C:/Users/soham/quemsah-awesome-claude-plugins", "adoption metrics"),
    ("claude-skills-llm-council", "C:/Users/soham/claude-skills-llm-council", "multi-advisor"),
    ("taste-skill", "C:/Users/soham/taste-skill", "anti-slop frontend"),
    ("design-motion-principles", "C:/Users/soham/design-motion-principles", "motion reference"),
    ("browser-harness", "C:/Users/soham/browser-harness", "real-browser control"),
]

CATEGORY_HINTS = {
    "ui": ["ui", "design", "frontend", "component", "tailwind", "css", "react", "vue", "svelte", "landing", "polish", "impeccable", "taste", "shadcn", "magic"],
    "animation": ["gsap", "motion", "animation", "scroll", "timeline", "transition"],
    "backend": ["backend", "api", "server", "django", "flask", "fastapi", "express", "node", "rails"],
    "database": ["sql", "postgres", "supabase", "firebase", "sqlite", "mongo", "redis", "db"],
    "devops": ["deploy", "docker", "kubernetes", "aws", "gcp", "azure", "ci", "cd", "terraform"],
    "testing": ["test", "tdd", "qa", "playwright", "vitest", "jest", "pytest"],
    "review": ["review", "audit", "lint", "critique", "pr"],
    "docs": ["docs", "documentation", "readme", "context7"],
    "memory": ["memory", "context", "session"],
    "browser": ["browser", "playwright", "puppeteer", "selenium", "scrape"],
    "ai": ["llm", "agent", "council", "claude", "anthropic", "openai"],
    "media": ["video", "audio", "ffmpeg", "music", "voice", "image", "remotion", "elevenlabs"],
    "workflow": ["loop", "ralph", "ship", "land", "deploy", "plan", "brainstorm"],
}


def _parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    raw = m.group(1)
    out: dict = {}
    current_key = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith(" ") and current_key:
            out[current_key] = (out.get(current_key, "") + " " + line.strip()).strip()
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            out[k] = v
            current_key = k
    return out


def _infer_category(name: str, description: str) -> list[str]:
    haystack = f"{name} {description}".lower()
    cats = [cat for cat, kws in CATEGORY_HINTS.items() if any(kw in haystack for kw in kws)]
    return cats or ["general"]


def _safe_read(path: Path, limit: int = 8192) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _scan_skill_dir(root: Path, source: str) -> Iterable[dict]:
    """Only top-level installed skills: <root>/<skill-name>/SKILL.md (depth 2).

    Deeper SKILL.md files belong to other-harness sub-bundles (e.g. gstack ships
    .cursor/, .factory/, .gbrain/ copies for other tools) and would inflate counts.
    """
    if not root.exists():
        return
    for skill_md in root.glob("*/SKILL.md"):
        if any(part.startswith(".") for part in skill_md.relative_to(root).parts):
            continue
        text = _safe_read(skill_md)
        fm = _parse_frontmatter(text)
        name = fm.get("name") or skill_md.parent.name
        desc = fm.get("description", "")[:400]
        yield {
            "id": f"skill:{source}:{name}",
            "type": "skill",
            "name": name,
            "description": desc,
            "source": source,
            "path": str(skill_md),
            "invoke": f"Skill({name})",
            "categories": _infer_category(name, desc),
        }


def _scan_plugins() -> Iterable[dict]:
    manifest = PLUGINS_DIR / "installed_plugins.json"
    if not manifest.exists():
        return
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for full_id, entries in (data.get("plugins") or {}).items():
        if not entries:
            continue
        plugin_name = full_id.split("@", 1)[0]
        marketplace = full_id.split("@", 1)[1] if "@" in full_id else "unknown"
        install_path = entries[0].get("installPath", "")
        desc = ""
        plugin_json = Path(install_path) / ".claude-plugin" / "plugin.json"
        if plugin_json.exists():
            try:
                pd = json.loads(plugin_json.read_text(encoding="utf-8"))
                desc = pd.get("description", "")[:400]
            except (OSError, json.JSONDecodeError):
                pass
        yield {
            "id": f"plugin:{full_id}",
            "type": "plugin",
            "name": plugin_name,
            "description": desc or f"plugin from {marketplace}",
            "source": marketplace,
            "path": install_path,
            "invoke": f"plugin:{plugin_name}",
            "categories": _infer_category(plugin_name, desc),
        }


def _scan_mcp_servers() -> Iterable[dict]:
    cfg = HOME / ".claude.json"
    if not cfg.exists():
        return
    try:
        with cfg.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return
    seen: set[str] = set()
    for name in (data.get("mcpServers") or {}):
        if name in seen:
            continue
        seen.add(name)
        yield {
            "id": f"mcp:{name}",
            "type": "mcp",
            "name": name,
            "description": f"MCP server: {name}",
            "source": "user",
            "path": "~/.claude.json",
            "invoke": f"mcp__{name}",
            "categories": _infer_category(name, ""),
        }
    for proj_path, proj in (data.get("projects") or {}).items():
        for name in (proj.get("mcpServers") or {}):
            key = f"{proj_path}::{name}"
            if key in seen:
                continue
            seen.add(key)
            yield {
                "id": f"mcp:project:{name}:{abs(hash(proj_path)) % 10**8}",
                "type": "mcp",
                "name": name,
                "description": f"MCP (project: {Path(proj_path).name})",
                "source": "project",
                "path": proj_path,
                "invoke": f"mcp__{name}",
                "categories": _infer_category(name, ""),
            }


def _scan_commands() -> Iterable[dict]:
    if not USER_COMMANDS_DIR.exists():
        return
    for md in USER_COMMANDS_DIR.rglob("*.md"):
        text = _safe_read(md)
        fm = _parse_frontmatter(text)
        name = md.stem
        desc = fm.get("description", "")[:300]
        yield {
            "id": f"command:{name}",
            "type": "command",
            "name": f"/{name}",
            "description": desc or "user slash command",
            "source": "user",
            "path": str(md),
            "invoke": f"/{name}",
            "categories": _infer_category(name, desc),
        }


def _scan_agents() -> Iterable[dict]:
    if not USER_AGENTS_DIR.exists():
        return
    for md in USER_AGENTS_DIR.rglob("*.md"):
        text = _safe_read(md)
        fm = _parse_frontmatter(text)
        name = fm.get("name") or md.stem
        desc = fm.get("description", "")[:300]
        yield {
            "id": f"agent:{name}",
            "type": "agent",
            "name": name,
            "description": desc,
            "source": "user",
            "path": str(md),
            "invoke": f"Agent({name})",
            "categories": _infer_category(name, desc),
        }


def _scan_discovery_repos() -> Iterable[dict]:
    for slug, root, label in DISCOVERY_REPOS:
        p = Path(root)
        if not p.exists():
            continue
        yield {
            "id": f"repo:{slug}",
            "type": "repo",
            "name": slug,
            "description": label,
            "source": "local-clone",
            "path": str(p),
            "invoke": f"reference:{slug}",
            "categories": _infer_category(slug, label),
        }


def _scan_plugin_internal_skills() -> Iterable[dict]:
    """Pull SKILL.md inside installed plugin caches (so plugins like superpowers, gstack expose their skills).

    Skips hidden subdirs (`.agents/`, `.cursor/`, etc.) — those are other-harness copies.
    """
    if not PLUGIN_CACHE.exists():
        return
    seen: set[str] = set()
    for skill_md in PLUGIN_CACHE.rglob("SKILL.md"):
        rel = skill_md.relative_to(PLUGIN_CACHE)
        if any(part.startswith(".") and part != ".claude-plugin" for part in rel.parts):
            continue
        text = _safe_read(skill_md)
        fm = _parse_frontmatter(text)
        name = fm.get("name") or skill_md.parent.name
        plugin_root = None
        for parent in skill_md.parents:
            if (parent / ".claude-plugin").exists():
                plugin_root = parent.name
                break
        key = f"{plugin_root}:{name}"
        if key in seen:
            continue
        seen.add(key)
        desc = fm.get("description", "")[:400]
        yield {
            "id": f"plugin-skill:{plugin_root}:{name}",
            "type": "skill",
            "name": name,
            "description": desc,
            "source": f"plugin:{plugin_root}" if plugin_root else "plugin",
            "path": str(skill_md),
            "invoke": f"Skill({name})",
            "categories": _infer_category(name, desc),
        }


def _attach_ratings(items: list[dict]) -> None:
    if toolforge_db is None:
        for it in items:
            it["rating"] = {"score": None, "n": 0, "avg": None}
        return
    names = [it["name"].lstrip("/") for it in items]
    try:
        bulk = toolforge_db.get_rating_stats_bulk(names)
    except Exception:  # noqa: BLE001
        bulk = {n: {"sum": 0, "n": 0, "avg": None, "decayed_avg": None} for n in names}
    for it in items:
        s = bulk.get(it["name"].lstrip("/"), {"n": 0, "avg": None, "decayed_avg": None})
        n = s.get("n") or 0
        decayed = s.get("decayed_avg")
        if n == 0 or decayed is None:
            score = None
        else:
            score = (decayed * n + 3.0 * 5.0) / (n + 5.0)
        it["rating"] = {
            "score": round(score, 2) if score is not None else None,
            "n": n,
            "avg": round(s["avg"], 2) if s.get("avg") is not None else None,
        }


def _dedupe(items: list[dict]) -> list[dict]:
    """Dedup by absolute path — every distinct on-disk source file is its own row."""
    seen: dict[str, dict] = {}
    for it in items:
        key = f"{it['type']}::{it['name']}::{it.get('path') or it['source']}"
        if key not in seen:
            seen[key] = it
    return list(seen.values())


def build_inventory() -> dict:
    items: list[dict] = []
    items.extend(_scan_skill_dir(USER_SKILLS_DIR, "user"))
    items.extend(_scan_plugin_internal_skills())
    items.extend(_scan_plugins())
    items.extend(_scan_mcp_servers())
    items.extend(_scan_commands())
    items.extend(_scan_agents())
    items.extend(_scan_discovery_repos())
    items = _dedupe(items)
    _attach_ratings(items)
    items.sort(key=lambda i: (i["type"], i["name"].lower()))

    type_counts: dict[str, int] = defaultdict(int)
    category_counts: dict[str, int] = defaultdict(int)
    for it in items:
        type_counts[it["type"]] += 1
        for c in it["categories"]:
            category_counts[c] += 1

    return {
        "items": items,
        "counts": {
            "total": len(items),
            "by_type": dict(type_counts),
            "by_category": dict(category_counts),
        },
    }


if __name__ == "__main__":
    out = build_inventory()
    json.dump(out, sys.stdout, indent=2)
