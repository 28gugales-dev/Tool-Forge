"""Scan local Claude install for skills, plugins, MCPs, agents, commands, discovery repos.

Joins each item with the toolforge.db Bayesian-shrunk rating so the UI can rank inline.
Pure stdlib. Returns JSON-serializable list. Failures degrade silent (missing dir => skip).

Adds curator capability ported from bin/toolforge_local_scan.py: category scoring,
recency_norm via git log or mtime, and a `curate(category, limit)` function the
curator skill can drive instead of shelling to local_scan.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
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

HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = HOME / ".claude"
PLUGINS_DIR = CLAUDE_DIR / "plugins"
MARKETPLACES_DIR = PLUGINS_DIR / "marketplaces"
USER_SKILLS_DIR = CLAUDE_DIR / "skills"
USER_COMMANDS_DIR = CLAUDE_DIR / "commands"
USER_AGENTS_DIR = CLAUDE_DIR / "agents"
PLUGIN_CACHE = PLUGINS_DIR / "cache"
CONFIG_PATH = CLAUDE_DIR / "toolforge-config.json"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)

# Curator constants (ported from bin/toolforge_local_scan.py)
MIN_CATEGORY_SCORE = 0.3
MAX_CURATE_PER_CATEGORY = 10
LOCAL_STARS_NORM = 0.4
DECAY_HALFLIFE_DAYS = 75.0  # AI tooling moves fast — was 180d, cut to ~2.5mo
RECENCY_SECONDS_PER_DAY = 86400.0
SUBPROCESS_TIMEOUT_SECONDS = 4.0

# Tight per-curator-category keyword sets. Distinct from the broader CATEGORY_HINTS
# below which is used for multi-label inventory tagging; CURATOR_KEYWORDS feeds the
# single-category 0-1 confidence score the curator skill ranks against.
CURATOR_KEYWORDS: dict[str, list[str]] = {
    "ui": [
        "ui", "frontend", "react", "vue", "svelte", "component", "css",
        "tailwind", "shadcn", "design", "layout", "animation", "motion",
        "theme", "font", "color", "typography", "magic", "aceternity",
        "21st", "framer", "gsap", "chakra", "mantine", "radix",
    ],
    "backend": [
        "backend", "api", "rest", "graphql", "server", "fastapi", "django",
        "flask", "express", "nestjs", "node", "python-backend", "ruby",
        "rails", "go", "rust", "microservice", "endpoint", "auth",
        "openapi", "swagger",
    ],
    "database": [
        "database", "db", "postgres", "postgresql", "sqlite", "mysql",
        "mariadb", "redis", "mongo", "mongodb", "sql", "schema",
        "migration", "orm", "prisma", "supabase", "firebase", "firestore",
        "drizzle", "knex", "data-engineer", "dataeng",
    ],
    "testing": [
        "test", "testing", "pytest", "vitest", "jest", "mocha", "e2e",
        "end-to-end", "playwright", "cypress", "selenium", "puppeteer",
        "mock", "fixture", "coverage", "lint", "qa", "snapshot",
        "test-automator", "tdd",
    ],
    "devops": [
        "devops", "docker", "container", "kubernetes", "k8s", "helm",
        "terraform", "ansible", "ci", "cd", "deploy", "deployment",
        "github-actions", "gitlab-ci", "aws", "gcp", "azure", "cloud",
        "infrastructure", "monitoring", "observability", "sre",
    ],
}
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


# Back-compat shim — historical callers may import DISCOVERY_REPOS as a constant.
# Realized lazily via resolver so config changes take effect without reimport.
DISCOVERY_REPOS = _resolved_discovery_repos()

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


def _recency_norm_from_path(raw_path: Optional[str]) -> float:
    """exp(-days/75) from `git log -1` (if entry sits in a git repo) else file mtime.
    Returns 1.0 when path is None (installed-only entry without a local file).
    """
    if not raw_path:
        return 1.0
    path = Path(raw_path)
    git_dir = path
    last_commit_epoch: Optional[float] = None
    try:
        while git_dir != git_dir.parent:
            if (git_dir / ".git").exists():
                break
            git_dir = git_dir.parent
        if (git_dir / ".git").exists():
            git_exe = shutil.which("git")
            if git_exe:
                proc = subprocess.run(
                    [git_exe, "-C", str(git_dir), "log", "-1", "--format=%ct", "--", str(path)],
                    shell=False, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=SUBPROCESS_TIMEOUT_SECONDS, check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    last_commit_epoch = float(proc.stdout.strip().splitlines()[0])
    except (OSError, ValueError, subprocess.TimeoutExpired, UnicodeDecodeError):
        last_commit_epoch = None
    if last_commit_epoch is None:
        try:
            last_commit_epoch = path.stat().st_mtime
        except OSError:
            return 0.0
    days = max(0.0, (time.time() - last_commit_epoch) / RECENCY_SECONDS_PER_DAY)
    return math.exp(-days / DECAY_HALFLIFE_DAYS)


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
    inv = build_inventory()
    out: list[dict] = []
    for it in inv.get("items", []):
        path_str = it.get("path") or ""
        cs = _curator_category_score(it["name"], it.get("description", ""), path_str, cat)
        if cs < MIN_CATEGORY_SCORE:
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
            "recency_norm": _recency_norm_from_path(it.get("path")),
            "rating": it.get("rating"),
        })
    out.sort(
        key=lambda x: (x["installed"], x["category_score"], x["recency_norm"]),
        reverse=True,
    )
    return out[:limit]


# ---------- CLI ----------

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
        assert _recency_norm_from_path(None) == 1.0
        print("OK: recency_norm None handling")
        passed += 1
    except AssertionError as exc:
        print(f"FAIL: recency_norm None: {exc}")
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
