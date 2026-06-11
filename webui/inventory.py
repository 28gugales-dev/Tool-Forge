"""Scan local Claude install for skills, plugins, MCPs, agents, commands, discovery repos.

Joins each item with the toolforge.db Bayesian-shrunk rating so the UI can rank inline.
Pure stdlib. Returns JSON-serializable list. Failures degrade silent (missing dir => skip).

Adds curator capability ported from bin/toolforge_local_scan.py: category scoring,
recency_norm via git log or mtime, and a `curate(category, limit)` function the
curator skill can drive instead of shelling to local_scan.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

try:
    import toolforge_db  # type: ignore
except Exception:  # noqa: BLE001
    toolforge_db = None  # graceful

# Canonical-source constant import. Hard import — no sane fallback for a numeric half-life.
from toolforge_db import DECAY_HALFLIFE_DAYS
from toolforge_local_scan import CATEGORY_KEYWORDS as CURATOR_KEYWORDS
from toolforge_local_scan import _parse_frontmatter, _recency_norm_from_path

# ---------- section: constants-and-paths ----------
HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = HOME / ".claude"
PLUGINS_DIR = CLAUDE_DIR / "plugins"
MARKETPLACES_DIR = PLUGINS_DIR / "marketplaces"
USER_SKILLS_DIR = CLAUDE_DIR / "skills"
USER_COMMANDS_DIR = CLAUDE_DIR / "commands"
USER_AGENTS_DIR = CLAUDE_DIR / "agents"
PLUGIN_CACHE = PLUGINS_DIR / "cache"
CONFIG_PATH = CLAUDE_DIR / "toolforge-config.json"

# Curator constants (ported from bin/toolforge_local_scan.py)
MIN_CATEGORY_SCORE = 0.3
MAX_CURATE_PER_CATEGORY = 10
LOCAL_STARS_NORM = 0.4
# WARN: see SKETCHY_CODE_AUDIT.md#s3-1 — FIXED in F17 (DECAY_HALFLIFE_DAYS imported from toolforge_db).
RECENCY_SECONDS_PER_DAY = 86400.0
SUBPROCESS_TIMEOUT_SECONDS = 4.0

# Tight per-curator-category keyword sets. Distinct from the broader CATEGORY_HINTS
# below which is used for multi-label inventory tagging; CURATOR_KEYWORDS feeds the
# single-category 0-1 confidence score the curator skill ranks against.
# WARN: see SKETCHY_CODE_AUDIT.md#s3-2 — FIXED in F18 (imported from toolforge_local_scan.CATEGORY_KEYWORDS as CURATOR_KEYWORDS).
SUPPORTED_CURATE_CATEGORIES = frozenset(CURATOR_KEYWORDS.keys())

# Default discovery repos (relative to HOME). Override via toolforge-config.json
# `discovery_repos`: [{ "slug": "...", "path": "/abs/or/~/relative", "label": "..." }, ...]
# Paths are expanded (~ + env vars) and skipped if they don't resolve to a real dir.
DEFAULT_DISCOVERY_REPOS: list[tuple[str, str, str]] = [
    ("everything-claude-code", "~/everything-claude-code", "comprehensive harness"),
    ("wshobson-agents", "~/wshobson-agents", "100+ domain agents"),
    ("agents-claude-code", "~/agents-claude-code", "104 role agents"),
    ("awesome-claude-agents", "~/awesome-claude-agents", "core+orchestrator agents"),
    ("claude-skills", "~/claude-skills", "domain skills"),
    ("antigravity-awesome-skills", "~/antigravity-awesome-skills", "writing/humanizer"),
    ("ruflo", "~/ruflo", "multi-agent swarms"),
    ("awesome-claude-code", "~/awesome-claude-code", "canonical discovery index"),
    ("andrej-karpathy-skills", "~/andrej-karpathy-skills", "CLAUDE.md template"),
    ("claude-mem", "~/claude-mem", "persistent cross-session memory"),
    ("alirezarezvani-claude-skills", "~/alirezarezvani-claude-skills", "245+ multi-domain"),
    ("composio-awesome-claude-plugins", "~/composio-awesome-claude-plugins", "Composio plugins"),
    ("composio-awesome-claude-skills", "~/composio-awesome-claude-skills", "Composio skills"),
    ("travisvn-awesome-claude-skills", "~/travisvn-awesome-claude-skills", "alt curation"),
    ("quemsah-awesome-claude-plugins", "~/quemsah-awesome-claude-plugins", "adoption metrics"),
    ("claude-skills-llm-council", "~/claude-skills-llm-council", "multi-advisor"),
    ("taste-skill", "~/taste-skill", "anti-slop frontend"),
    ("design-motion-principles", "~/design-motion-principles", "motion reference"),
    ("browser-harness", "~/browser-harness", "real-browser control"),
]


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"inventory: config unreadable, using defaults: {exc}", file=sys.stderr)
        return {}


# Note: prior DISCOVERY_REPOS module-level alias removed (FIXED in F47, see SKETCHY_CODE_AUDIT.md#s7-2).
def _resolved_discovery_repos() -> list[tuple[str, str, str]]:
    """Read discovery repos from config, fall back to HOME-relative defaults.

    Both lists are filtered to entries whose path actually exists, so an empty
    machine yields an empty list without erroring.
    """
    cfg = _load_config()
    raw = cfg.get("discovery_repos")
    candidates: list[tuple[str, str, str]] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            slug = entry.get("slug")
            path = entry.get("path")
            label = entry.get("label", "")
            if isinstance(slug, str) and isinstance(path, str) and slug and path:
                candidates.append((slug, path, str(label)))
    if not candidates:
        candidates = list(DEFAULT_DISCOVERY_REPOS)
    out: list[tuple[str, str, str]] = []
    for slug, raw_path, label in candidates:
        expanded = os.path.expandvars(os.path.expanduser(raw_path))
        if Path(expanded).is_dir():
            out.append((slug, expanded, label))
    return out


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


# ---------- section: frontmatter-parsing ----------
# WARN: see SKETCHY_CODE_AUDIT.md#s3-4 — FIXED in F20 (_parse_frontmatter imported from toolforge_local_scan).


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


# ---------- section: skill-scanners ----------
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


# ---------- section: plugin-scanners ----------
def _scan_plugins() -> Iterable[dict]:
    manifest = PLUGINS_DIR / "installed_plugins.json"
    if not manifest.exists():
        return
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errorId = uuid.uuid4().hex[:8]
        print(
            f"inventory: plugin manifest unreadable errorId={errorId} "
            f"path={manifest} cls={type(exc).__name__} msg={exc}",
            file=sys.stderr,
        )
        yield {
            "_inventory_warning": f"plugin manifest unreadable: {manifest}: {exc}",
            "_errorId": errorId,
        }
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


# ---------- section: mcp-scanners ----------
def _scan_mcp_servers() -> Iterable[dict]:
    cfg = HOME / ".claude.json"
    if not cfg.exists():
        return
    try:
        with cfg.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        errorId = uuid.uuid4().hex[:8]
        print(
            f"inventory: mcp config unreadable errorId={errorId} "
            f"path={cfg} cls={type(exc).__name__} msg={exc}",
            file=sys.stderr,
        )
        yield {
            "_inventory_warning": f"mcp config unreadable: {cfg}: {exc}",
            "_errorId": errorId,
        }
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
                # WARN: see SKETCHY_CODE_AUDIT.md#s3-8 — FIXED in F24 (sha1 hex prefix, stable across restarts).
                "id": f"mcp:project:{name}:{hashlib.sha1(proj_path.encode('utf-8')).hexdigest()[:12]}",
                "type": "mcp",
                "name": name,
                "description": f"MCP (project: {Path(proj_path).name})",
                "source": "project",
                "path": proj_path,
                "invoke": f"mcp__{name}",
                "categories": _infer_category(name, ""),
            }


# ---------- section: command-and-agent-scanners ----------
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


# ---------- section: discovery-repos ----------
def _scan_discovery_repos() -> Iterable[dict]:
    # Re-resolve each call so config edits propagate without reimport.
    for slug, root, label in _resolved_discovery_repos():
        yield {
            "id": f"repo:{slug}",
            "type": "repo",
            "name": slug,
            "description": label,
            "source": "local-clone",
            "path": root,
            "invoke": f"reference:{slug}",
            "categories": _infer_category(slug, label),
        }


# ---------- section: plugin-scanners (cont'd: plugin-internal skills) ----------
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


# ---------- section: dedupe-and-rating ----------
def _attach_ratings(items: list[dict]) -> list[dict]:
    """Annotate items with rating data in-place. Returns warnings list (may be empty).

    A non-empty warnings list signals DB lookup failure — caller MUST forward to
    the UI so users can distinguish "no ratings yet" (prior_only) from "DB
    unavailable" (rating_unavailable).
    """
    warnings: list[dict] = []
    if toolforge_db is None:
        for it in items:
            it["rating"] = {"score": None, "n": 0, "avg": None}
        return warnings
    names = [it["name"].lstrip("/") for it in items]
    # WARN: see SKETCHY_CODE_AUDIT.md#s2-1 — FIXED in F08 (DB error surfaced via warnings list to UI).
    try:
        bulk = toolforge_db.get_rating_stats_bulk(names)
    except (sqlite3.Error, OSError, ValueError) as exc:
        errorId = uuid.uuid4().hex[:8]
        print(
            f"inventory: rating bulk lookup failed errorId={errorId} "
            f"cls={type(exc).__name__} msg={exc}",
            file=sys.stderr,
        )
        bulk = {n: {"sum": 0, "n": 0, "avg": None, "decayed_avg": None,
                    "_error": str(exc), "_errorId": errorId} for n in names}
    if isinstance(bulk, dict) and "_error" in bulk:
        warnings.append({"type": "rating_unavailable", "detail": str(bulk["_error"])})
    else:
        # Per-name nested _error (set by the except branch above). Collapse to a
        # single UI warning — every entry carries the same exception string.
        nested_errors = {
            str(v["_error"])
            for v in bulk.values()
            if isinstance(v, dict) and "_error" in v
        }
        for detail in nested_errors:
            warnings.append({"type": "rating_unavailable", "detail": detail})
    prior_mean = getattr(toolforge_db, "BAYES_PRIOR_MEAN", 3.0)
    prior_weight = getattr(toolforge_db, "BAYES_PRIOR_WEIGHT", 5.0)
    prior_norm = prior_mean / 5.0  # normalized prior (mean 3 / max 5 = 0.6)
    for it in items:
        s = bulk.get(it["name"].lstrip("/"), {"n": 0, "avg": None, "decayed_avg": None})
        n = s.get("n") or 0
        decayed = s.get("decayed_avg")
        if n == 0 or decayed is None:
            # No ratings yet: surface the Bayesian prior (0.6) so the UI matches
            # the curator skill's ranking. Curator and webui MUST agree on n=0.
            score = prior_norm
            score_basis = "prior_only"
        else:
            posterior = (decayed * n + prior_mean * prior_weight) / (n + prior_weight)
            score = posterior / 5.0
            score_basis = "weighted"
        it["rating"] = {
            "score": round(score, 2),
            "score_basis": score_basis,
            "n": n,
            "n_actual": n,
            "avg": round(s["avg"], 2) if s.get("avg") is not None else None,
        }
    return warnings


# WARN: see SKETCHY_CODE_AUDIT.md#s5-6 — FIXED in F43 (docstring now matches actual key shape).
def _dedupe(items: list[dict]) -> list[dict]:
    """Dedup by (type, name, path or source) tuple. Plugin/mcp entries fall back to source label when path is empty."""
    seen: dict[str, dict] = {}
    for it in items:
        key = f"{it['type']}::{it['name']}::{it.get('path') or it['source']}"
        if key not in seen:
            seen[key] = it
    return list(seen.values())


# ---------- section: public-api ----------
# TTL cache: build_inventory() runs 7 filesystem scans (incl. PLUGIN_CACHE.rglob
# over the whole plugin tree) + a DB ratings query — full cost on every UI refresh.
# Cache the result for INVENTORY_CACHE_TTL seconds. Lock guards the dict swap only,
# NOT the scan: a duplicate concurrent scan under racing threads is acceptable
# (idempotent, read-only); torn state (built_at without matching data) is not.
INVENTORY_CACHE_TTL = 20.0  # seconds
_inventory_cache: dict = {"built_at": 0.0, "data": None}
_inventory_cache_lock = threading.Lock()


def invalidate_inventory_cache() -> None:
    """Drop the cached inventory so the next build_inventory() re-scans.

    Call after any IN-PROCESS mutation that changes what the inventory reports
    (e.g. a skill export writes a new SKILL.md, a flow delete removes an
    exported skill). Mutations from other processes (CLI installs, hook
    rating writes via toolforge_db) cannot reach this cache — they ride the
    INVENTORY_CACHE_TTL window by design.
    """
    with _inventory_cache_lock:
        _inventory_cache["built_at"] = 0.0
        _inventory_cache["data"] = None


def build_inventory(force: bool = False) -> dict:
    """Return the full inventory dict, served from a TTL cache when warm.

    force=True bypasses the cache (fresh scan) and refreshes it. Output shape is
    unchanged from the underlying scan.
    """
    if not force:
        # Read both fields under the lock so data/built_at always pair up
        # (server is ThreadingHTTPServer — concurrent readers + invalidation).
        with _inventory_cache_lock:
            cached = _inventory_cache["data"]
            built_at = _inventory_cache["built_at"]
        if cached is not None and (time.monotonic() - built_at) < INVENTORY_CACHE_TTL:
            return cached
    data = _build_inventory()
    with _inventory_cache_lock:
        _inventory_cache["data"] = data
        _inventory_cache["built_at"] = time.monotonic()
    return data


def _build_inventory() -> dict:
    raw: list[dict] = []
    raw.extend(_scan_skill_dir(USER_SKILLS_DIR, "user"))
    raw.extend(_scan_plugin_internal_skills())
    raw.extend(_scan_plugins())
    raw.extend(_scan_mcp_servers())
    raw.extend(_scan_commands())
    raw.extend(_scan_agents())
    raw.extend(_scan_discovery_repos())
    warnings = [r for r in raw if "_inventory_warning" in r]
    items = [r for r in raw if "_inventory_warning" not in r]
    items = _dedupe(items)
    rating_warnings = _attach_ratings(items)
    warnings.extend(rating_warnings)
    items.sort(key=lambda i: (i["type"], i["name"].lower()))

    type_counts: dict[str, int] = defaultdict(int)
    category_counts: dict[str, int] = defaultdict(int)
    for it in items:
        type_counts[it["type"]] += 1
        for c in it["categories"]:
            category_counts[c] += 1

    return {
        "items": items,
        "warnings": warnings,
        "counts": {
            "total": len(items),
            "by_type": dict(type_counts),
            "by_category": dict(category_counts),
        },
    }


# ---------- Curator capability (ported from bin/toolforge_local_scan.py) ----------

def _curator_category_score(name: str, description: str, path: str, category: str) -> float:
    """0-1 confidence that an entry belongs to <category>. Mirrors local_scan logic
    so existing curator-skill ranking output is preserved.
    """
    if category not in CURATOR_KEYWORDS:
        return 0.0
    keywords = CURATOR_KEYWORDS[category]
    text = f" {name.lower()} {description.lower()} {path.lower().replace(os.sep, ' ')} "
    score = 0.0
    if f" {category} " in text or f"-{category}-" in text or f"/{category}/" in text:
        score += 0.5
    kw_hits = 0
    for kw in keywords:
        if (f" {kw} " in text or f"-{kw}" in text or f"{kw}-" in text or f"/{kw}/" in text):
            kw_hits += 1
            if kw_hits >= 5:
                break
    score += min(0.5, 0.1 * kw_hits)
    return min(1.0, score)


def curate(category: str, limit: int = MAX_CURATE_PER_CATEGORY) -> list[dict]:
    """Return entries scored against <category>, filtered ≥ MIN_CATEGORY_SCORE,
    ranked by (installed, category_score, recency_norm) desc, capped at <limit>.

    Output schema mirrors what bin/toolforge_local_scan.py emitted so the curator
    skill can swap callers without changing its parsing.
    """
    cat = category.lower()
    if cat not in SUPPORTED_CURATE_CATEGORIES:
        raise ValueError(
            f"unsupported curator category {category!r}: "
            f"pick one of {sorted(SUPPORTED_CURATE_CATEGORIES)}"
        )
    inv = build_inventory(force=True)
    out: list[dict] = []
    for it in inv.get("items", []):
        path_str = it.get("path") or ""
        cs = _curator_category_score(it["name"], it.get("description", ""), path_str, cat)
        if cs < MIN_CATEGORY_SCORE:
            continue
        # Stat-failure tools are dropped: the curator skill's locked schema requires
        # recency_norm in [0.0, 1.0] and its composite uses `recency_norm * 0.3`.
        # Putting 0.0 would silently hide a broken scan as "an old tool". Stderr
        # warning has already been logged inside _recency_norm_from_path.
        # Installed-only entries without a local file (no path) get recency=1.0;
        # the canonical _recency_norm_from_path takes Path (not Optional[str]),
        # so the None→1.0 semantic is owned here at the call site (F19).
        item_path = it.get("path")
        if not item_path:
            recency: Optional[float] = 1.0
        else:
            recency = _recency_norm_from_path(Path(item_path))
        if recency is None:
            continue
        installed = it["type"] in ("skill", "plugin", "mcp", "command", "agent")
        out.append({
            "name": it["name"],
            "type": it["type"],
            "source": it["source"],
            "path": it.get("path"),
            "installed": installed,
            "description": it.get("description", ""),
            "category": cat,
            "category_score": cs,
            "stars_norm": LOCAL_STARS_NORM,
            "recency_norm": recency,
            "rating": it.get("rating"),
        })
    out.sort(
        key=lambda x: (x["installed"], x["category_score"], x["recency_norm"]),
        reverse=True,
    )
    return out[:limit]


# ---------- section: cli-entry-point ----------

def _usage() -> str:
    return (
        "Usage:\n"
        "  python inventory.py                       # full inventory JSON\n"
        "  python inventory.py curate <category>     # ranked candidates for category\n"
        "  python inventory.py --self-test           # run smoke tests\n"
        f"  categories: {sorted(SUPPORTED_CURATE_CATEGORIES)}\n"
    )


def _self_test() -> int:
    passed = failed = 0
    try:
        s = _curator_category_score(
            "shadcn-ui-mcp", "shadcn React components MCP",
            "/x/y/ui/shadcn.md", "ui",
        )
        assert s >= 0.5, f"shadcn UI scored {s}, expected >= 0.5"
        s2 = _curator_category_score(
            "postgres-mcp", "postgres database tools",
            "/x/y/postgres.md", "ui",
        )
        assert s2 < MIN_CATEGORY_SCORE, f"postgres should not match ui ({s2})"
        s3 = _curator_category_score(
            "postgres-mcp", "postgres database tools",
            "/x/y/postgres.md", "database",
        )
        assert s3 >= 0.5
        print("OK: curator category scoring")
        passed += 1
    except AssertionError as exc:
        print(f"FAIL: curator category scoring: {exc}")
        failed += 1
    try:
        # F19: None→1.0 semantic moved to call site (curate()); canonical fn requires Path.
        # Verify the curate() call site short-circuits to 1.0 when item.path is falsy.
        # (Functional equivalent of the prior `_recency_norm_from_path(None) == 1.0` assertion.)
        path_str = ""
        recency = 1.0 if not path_str else _recency_norm_from_path(Path(path_str))
        assert recency == 1.0
        print("OK: recency_norm None-path handling at call site (F19)")
        passed += 1
    except AssertionError as exc:
        print(f"FAIL: recency_norm None-path: {exc}")
        failed += 1
    try:
        # M6: stat() failure must return None (not 0.0). Monkey-patch Path.stat to
        # raise PermissionError, confirm None flows through. Use a non-git path so
        # the git-log branch is skipped and we hit the stat fallback.
        import pathlib as _pl
        original_stat = _pl.Path.stat
        def _boom(self, *args, **kwargs):
            raise PermissionError("EACCES — simulated")
        _pl.Path.stat = _boom  # type: ignore[assignment]
        try:
            result = _recency_norm_from_path(Path("/nonexistent/no-git-here/file.md"))
        finally:
            _pl.Path.stat = original_stat  # type: ignore[assignment]
        assert result is None, f"expected None on stat failure, got {result!r}"
        print("OK: recency_norm stat-failure returns None")
        passed += 1
    except AssertionError as exc:
        print(f"FAIL: recency_norm stat failure: {exc}")
        failed += 1
    try:
        try:
            curate("not_a_category")
            print("FAIL: invalid category accepted")
            failed += 1
        except ValueError:
            print("OK: invalid category rejected")
            passed += 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: invalid category: {exc}")
        failed += 1
    try:
        repos = _resolved_discovery_repos()
        assert isinstance(repos, list)
        for slug, path, label in repos:
            assert isinstance(slug, str) and slug
            assert isinstance(path, str) and Path(path).is_dir()
            assert isinstance(label, str)
        print(f"OK: discovery_repos resolved ({len(repos)} live)")
        passed += 1
    except AssertionError as exc:
        print(f"FAIL: discovery_repos: {exc}")
        failed += 1
    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] in ("-h", "--help"):
        print(_usage(), file=sys.stderr)
        return 2
    if len(argv) >= 2 and argv[1] == "--self-test":
        return _self_test()
    if len(argv) >= 2 and argv[1] == "curate":
        if len(argv) < 3:
            print(_usage(), file=sys.stderr)
            return 2
        try:
            result = curate(argv[2])
        except ValueError as exc:
            print(f"inventory: {exc}", file=sys.stderr)
            return 2
        json.dump(result, sys.stdout, indent=2)
        return 0
    out = build_inventory()
    json.dump(out, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
