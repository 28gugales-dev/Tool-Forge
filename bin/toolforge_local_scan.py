#!/usr/bin/env python3
"""ToolForge local-source scanner.

Scans the user's machine for already-installed Claude tools plus optionally
user-configured local reference repos, returning a ranked candidate list per
category. Output is the same per-entry schema the curator skill consumes for
web-discovered candidates, with extra fields:

  - source       : provenance label (installed-plugin|installed-mcp|user-skills|...)
  - path         : absolute path on disk (or null if subprocess-derived)
  - installed    : True if the user can already invoke this tool today
  - category_score : 0.0-1.0 keyword-match confidence
  - stars_norm   : fixed 0.4 for local sources (no GitHub stars to read)
  - recency_norm : exp(-days/75) from `git log -1` if path is a git repo, else file mtime

Config: ~/.claude/toolforge-config.json
  { "local_paths": ["/path/one", "/path/two"] }

Defaults (when config absent or missing local_paths key):
  ~/.claude/skills, ~/.claude/agents, ./.claude/skills, ./.claude/agents

Cache: tempdir/toolforge_local_scan_<category>.json, 5-min TTL. Pass --force to bypass.

Security boundaries:
  - No symlink follow.
  - Per-file read cap: 4 KiB (descriptions live in frontmatter / first lines).
  - Hard cap on files scanned: 2000.
  - Subprocess: `claude plugin list`, `claude mcp list`, `git log -1 --format=%ct` — all shell=False with explicit timeouts.
  - Path traversal: configured paths are resolved + canonicalized; entries that escape
    the resolved root (via .. or symlink) are dropped.
"""
# WARN: see SKETCHY_CODE_AUDIT.md#s5-7 — FIXED in F44 (git log subprocess now named in docstring).

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# Sibling bin/ import — script-dir is on sys.path when invoked as `python toolforge_local_scan.py`.
from toolforge_db import DECAY_HALFLIFE_DAYS
from toolforge_usage_detector import _normalize_name  # noqa: F401  # re-exported for legacy callers

# ---------- section: constants ----------
CACHE_TTL_SECONDS = 300
MAX_FILES_SCANNED = 2000
MAX_FILE_READ_BYTES = 4096
MAX_DEPTH = 4
LOCAL_STARS_NORM = 0.4
# WARN: see SKETCHY_CODE_AUDIT.md#s3-1 — FIXED in F17 (DECAY_HALFLIFE_DAYS imported from toolforge_db).
RECENCY_SECONDS_PER_DAY = 86400.0
MIN_CATEGORY_SCORE = 0.3
MAX_LOCAL_PER_CATEGORY = 10
SCAN_BUDGET_SECONDS = 8.0
# WARN: see SKETCHY_CODE_AUDIT.md#s6-1 — FIXED in F37 (code now matches ARCHITECTURE.md §4.5/§5.4: 5s for claude list, 2s for git log).
GIT_LOG_TIMEOUT_SECONDS = 2.0
CLAUDE_LIST_TIMEOUT_SECONDS = 5.0
DESCRIPTION_MAX_CHARS = 200

CONFIG_PATH = Path(os.path.expanduser("~/.claude/toolforge-config.json"))

# WARN: see SKETCHY_CODE_AUDIT.md#s3-2 — FIXED in F18 (canonical source; imported by webui/inventory.py).
CATEGORY_KEYWORDS: dict[str, list[str]] = {
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

SUPPORTED_CATEGORIES = frozenset(CATEGORY_KEYWORDS.keys())
TOOL_NAME_RE = re.compile(r"^[a-z0-9._@/-]{1,80}$")

FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)
FRONTMATTER_DESCRIPTION_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


# ---------- section: config-loader ----------
def _load_config() -> dict:
    path = CONFIG_PATH
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return data
    # WARN: see SKETCHY_CODE_AUDIT.md#s4-1 — FIXED in F25 (corrupt config quarantined like _load_cache does).
    except json.JSONDecodeError as exc:
        quarantine = path.with_suffix(f".corrupt.{int(time.time())}")
        try:
            path.rename(quarantine)
        except OSError:
            pass
        print(f"toolforge_local_scan: corrupt config quarantined to {quarantine}: {exc}", file=sys.stderr)
        return {}
    except OSError as exc:
        print(f"toolforge_local_scan: config unreadable, using defaults: {exc}", file=sys.stderr)
        return {}


def _default_local_paths() -> list[Path]:
    paths: list[Path] = []
    for cand in [
        Path(os.path.expanduser("~/.claude/skills")),
        Path(os.path.expanduser("~/.claude/agents")),
        Path.cwd() / ".claude" / "skills",
        Path.cwd() / ".claude" / "agents",
    ]:
        if cand.exists():
            paths.append(cand.resolve())
    return paths


def _resolved_local_paths() -> list[Path]:
    cfg = _load_config()
    raw = cfg.get("local_paths")
    if not isinstance(raw, list):
        return _default_local_paths()
    resolved: list[Path] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        p = Path(os.path.expanduser(entry))
        try:
            real = p.resolve()
        except OSError:
            continue
        if real.exists() and real.is_dir():
            resolved.append(real)
    if not resolved:
        return _default_local_paths()
    return resolved


# ---------- section: metadata-extractors ----------
# WARN: see SKETCHY_CODE_AUDIT.md#s3-5 — FIXED in F21 (canonical _normalize_name imported from toolforge_usage_detector at module top).


def _truncate_desc(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > DESCRIPTION_MAX_CHARS:
        return text[: DESCRIPTION_MAX_CHARS - 3] + "..."
    return text


def _read_capped(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_FILE_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


# WARN: see SKETCHY_CODE_AUDIT.md#s3-4 — FIXED in F20 (canonical source; imported by webui/inventory.py).
def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out: dict[str, str] = {}
    nm = FRONTMATTER_NAME_RE.search(block)
    if nm:
        out["name"] = nm.group(1).strip("'\" ")
    desc = FRONTMATTER_DESCRIPTION_RE.search(block)
    if desc:
        out["description"] = desc.group(1).strip("'\" ")
    return out


# ---------- section: scoring-helpers ----------
def _category_score(name: str, description: str, path: Path, category: str) -> float:
    keywords = CATEGORY_KEYWORDS[category]
    text = f" {name.lower()} {description.lower()} {str(path).lower().replace(os.sep, ' ')} "
    score = 0.0
    if f" {category} " in text or f"-{category}-" in text or f"/{category}/" in text:
        score += 0.5
    kw_hits = 0
    for kw in keywords:
        if f" {kw} " in text or f"-{kw}" in text or f"{kw}-" in text or f"/{kw}/" in text:
            kw_hits += 1
            if kw_hits >= 5:
                break
    score += min(0.5, 0.1 * kw_hits)
    return min(1.0, score)


# WARN: see SKETCHY_CODE_AUDIT.md#s3-3 — FIXED in F19 (canonical source; imported by webui/inventory.py).
def _recency_norm_from_path(path: Path) -> Optional[float]:
    """exp(-days/75) from git log when path is in a repo, else file mtime.
    Returns None on stat() failure (disconnected drive / EACCES / etc.) — caller
    MUST exclude the entry from scan output. Curator skill's locked schema requires
    recency_norm in [0.0, 1.0]; a 0.0 sentinel was indistinguishable from a genuinely
    stale tool, so the entry is dropped with a stderr warning instead.
    """
    git_dir = path
    while git_dir != git_dir.parent:
        if (git_dir / ".git").exists():
            break
        git_dir = git_dir.parent
    last_commit_epoch: Optional[float] = None
    if (git_dir / ".git").exists():
        git_exe = shutil.which("git")
        if git_exe:
            try:
                proc = subprocess.run(
                    [git_exe, "-C", str(git_dir), "log", "-1", "--format=%ct", "--", str(path)],
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=GIT_LOG_TIMEOUT_SECONDS,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout and proc.stdout.strip():
                    last_commit_epoch = float(proc.stdout.strip().splitlines()[0])
            except (OSError, ValueError, subprocess.TimeoutExpired, UnicodeDecodeError):
                pass
    if last_commit_epoch is None:
        try:
            last_commit_epoch = path.stat().st_mtime
        except OSError as exc:
            import uuid
            errorId = uuid.uuid4().hex[:8]
            print(
                f"toolforge: stat failed for {path}: {exc} (recency unknown) "
                f"errorId={errorId} cls={type(exc).__name__}",
                file=sys.stderr,
            )
            return None
    days = max(0.0, (time.time() - last_commit_epoch) / RECENCY_SECONDS_PER_DAY)
    return math.exp(-days / DECAY_HALFLIFE_DAYS)


# ---------- section: entry-builders ----------
def _entry_from_skill_md(path: Path, source: str, root: Path) -> Optional[dict]:
    text = _read_capped(path)
    if not text:
        return None
    fm = _parse_frontmatter(text)
    raw_name = fm.get("name") or path.parent.name
    name = _normalize_name(raw_name)
    if not name or not TOOL_NAME_RE.match(name):
        return None
    description = _truncate_desc(fm.get("description", ""))
    return {
        "name": name,
        "type": "skill",
        "source": source,
        "path": str(path),
        "installed": source.startswith("installed-") or source in {"user-skills", "user-agents"},
        "description": description or f"local skill at {path.relative_to(root)}",
    }


def _entry_from_plugin_json(path: Path, source: str, root: Path) -> Optional[dict]:
    text = _read_capped(path)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    raw_name = data.get("name", path.parent.name) if isinstance(data, dict) else path.parent.name
    name = _normalize_name(str(raw_name))
    if not name or not TOOL_NAME_RE.match(name):
        return None
    description = ""
    if isinstance(data, dict):
        description = _truncate_desc(str(data.get("description", "")))
    return {
        "name": name,
        "type": "plugin",
        "source": source,
        "path": str(path),
        "installed": source.startswith("installed-"),
        "description": description or f"local plugin at {path.relative_to(root)}",
    }


# ---------- section: scanners ----------
def _iter_relevant_files(root: Path, deadline: float) -> list[tuple[Path, str]]:
    """Walk root with depth + count + deadline caps. No symlink follow."""
    results: list[tuple[Path, str]] = []
    count = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        if time.monotonic() > deadline or count >= MAX_FILES_SCANNED:
            break
        current, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    # WARN: see SKETCHY_CODE_AUDIT.md#s4-3 — FIXED in F27 (check fires before increment; stderr warns at cap).
                    if count >= MAX_FILES_SCANNED:
                        print(f"toolforge_local_scan: file-count cap reached at {MAX_FILES_SCANNED}", file=sys.stderr)
                        break
                    count += 1
                    p = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in {"node_modules", ".git", "__pycache__", "dist", ".vite", ".venv", "venv"}:
                            continue
                        stack.append((p, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        if p.name == "SKILL.md":
                            results.append((p, "skill"))
                        elif p.name == "plugin.json":
                            results.append((p, "plugin"))
        # WARN: see SKETCHY_CODE_AUDIT.md#s4-2 — FIXED in F26 (stderr-warn before continuing).
        except OSError as exc:
            print(f"toolforge_local_scan: unreadable subdir {current!r}: {exc}", file=sys.stderr)
            continue
    return results


def _scan_local_paths(category: str, deadline: float) -> list[dict]:
    out: list[dict] = []
    for root in _resolved_local_paths():
        source_label = f"local-repo:{root}"
        if root == Path(os.path.expanduser("~/.claude/skills")).resolve():
            source_label = "user-skills"
        elif root == Path(os.path.expanduser("~/.claude/agents")).resolve():
            source_label = "user-agents"
        elif root.name == "skills" and root.parent.name == ".claude":
            source_label = "project-skills"
        elif root.name == "agents" and root.parent.name == ".claude":
            source_label = "project-agents"
        for path, kind in _iter_relevant_files(root, deadline):
            if time.monotonic() > deadline:
                break
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if kind == "skill":
                entry = _entry_from_skill_md(path, source_label, root)
            else:
                entry = _entry_from_plugin_json(path, source_label, root)
            if not entry:
                continue
            entry["category_score"] = _category_score(
                entry["name"], entry["description"], Path(entry["path"]), category
            )
            if entry["category_score"] < MIN_CATEGORY_SCORE:
                continue
            # Stat-failure entries are dropped: the curator skill's locked schema
            # requires recency_norm in [0.0, 1.0] and composite scoring uses
            # `recency_norm * 0.3`. Emitting 0.0 would silently hide a broken scan
            # as "an old tool". Stderr warning logged inside _recency_norm_from_path.
            recency = _recency_norm_from_path(Path(entry["path"]))
            if recency is None:
                continue
            entry["category"] = category
            entry["stars_norm"] = LOCAL_STARS_NORM
            entry["recency_norm"] = recency
            out.append(entry)
    return out


# Header regex for plain-text fallback. Anchored to full-width column names so
# plugins whose names start with "Plugin"/"Name" are not mistakenly dropped.
_PLUGIN_LIST_HEADER_RE = re.compile(r"^\s*Plugin\s+Version\s+Source\b", re.IGNORECASE)


def _scan_installed_plugins() -> list[dict]:
    out: list[dict] = []
    claude_exe = shutil.which("claude")
    if not claude_exe:
        return out

    # Primary path: --json mode (supported per https://code.claude.com/docs/en/plugins-reference).
    try:
        proc_json = subprocess.run(
            [claude_exe, "plugin", "list", "--json"],
            shell=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=CLAUDE_LIST_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        print(f"toolforge_local_scan: claude plugin list --json failed: {exc}", file=sys.stderr)
        return out

    if proc_json.returncode == 0 and proc_json.stdout:
        try:
            data = json.loads(proc_json.stdout)
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    raw_name = item.get("name") or item.get("plugin") or ""
                    nm = _normalize_name(str(raw_name))
                    if nm and TOOL_NAME_RE.match(nm):
                        out.append({
                            "name": nm,
                            "type": "plugin",
                            "source": "installed-plugin",
                            "path": None,
                            "installed": True,
                            "description": f"installed Claude Code plugin: {nm}",
                        })
            return out
        except json.JSONDecodeError:
            print(
                "toolforge_local_scan: claude plugin list --json returned non-JSON; "
                "falling back to plain-text parser",
                file=sys.stderr,
            )

    # Fallback: plain-text parse for older CLI versions without --json.
    try:
        proc = subprocess.run(
            [claude_exe, "plugin", "list"],
            shell=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=CLAUDE_LIST_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        print(f"toolforge_local_scan: claude plugin list failed: {exc}", file=sys.stderr)
        return out
    if proc.returncode != 0 or not proc.stdout:
        return out
    for line in proc.stdout.splitlines():
        line = line.strip()
        # WARN: see SKETCHY_CODE_AUDIT.md#s4-10 — FIXED in F32 (--json mode primary; stricter header regex in fallback).
        if not line or line.startswith(("---", "===")) or _PLUGIN_LIST_HEADER_RE.match(line):
            continue
        first = line.split(None, 1)[0]
        nm = _normalize_name(first)
        if nm and TOOL_NAME_RE.match(nm):
            out.append({
                "name": nm,
                "type": "plugin",
                "source": "installed-plugin",
                "path": None,
                "installed": True,
                "description": f"installed Claude Code plugin: {nm}",
            })
    return out


def _scan_installed_mcps() -> list[dict]:
    out: list[dict] = []
    claude_exe = shutil.which("claude")
    if not claude_exe:
        return out
    try:
        proc = subprocess.run(
            [claude_exe, "mcp", "list"],
            shell=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=CLAUDE_LIST_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        print(f"toolforge_local_scan: claude mcp list failed: {exc}", file=sys.stderr)
        return out
    if proc.returncode != 0 or not proc.stdout:
        return out
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(("---", "===", "NAME", "Name")):
            continue
        first = line.split(":", 1)[0].split(None, 1)[0]
        nm = _normalize_name(first)
        if nm and TOOL_NAME_RE.match(nm):
            out.append({
                "name": nm,
                "type": "mcp",
                "source": "installed-mcp",
                "path": None,
                "installed": True,
                "description": f"installed MCP server: {nm}",
            })
    return out


def _enrich_installed(entries: list[dict], category: str) -> list[dict]:
    out = []
    for e in entries:
        e["category_score"] = _category_score(e["name"], e["description"], Path(e["path"] or "."), category)
        if e["category_score"] < MIN_CATEGORY_SCORE:
            continue
        e["category"] = category
        e["stars_norm"] = LOCAL_STARS_NORM
        e["recency_norm"] = 1.0
        out.append(e)
    return out


def _dedupe_and_cap(entries: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for e in entries:
        key = e["name"]
        if key not in seen:
            seen[key] = e
            continue
        prev = seen[key]
        if e["installed"] and not prev["installed"]:
            seen[key] = e
        elif e["category_score"] > prev["category_score"]:
            seen[key] = e
    ranked = sorted(
        seen.values(),
        key=lambda x: (x["installed"], x["category_score"], x["recency_norm"]),
        reverse=True,
    )
    return ranked[:MAX_LOCAL_PER_CATEGORY]


# ---------- section: cache ----------
def _cache_path(category: str) -> Path:
    return Path(tempfile.gettempdir()) / f"toolforge_local_scan_{category}.json"


def _load_cache(category: str) -> Optional[list[dict]]:
    path = _cache_path(category)
    if not path.exists():
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if (time.time() - st.st_mtime) > CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Corruption (partial write, disk error, tampering). Quarantine instead
        # of unlink so forensic evidence survives. Re-scan on return.
        print(
            f"toolforge: cache corrupt at {path}, re-scanning: "
            f"{exc.msg} at line {exc.lineno} col {exc.colno}",
            file=sys.stderr,
        )
        quarantine = path.with_suffix(path.suffix + f".corrupt.{int(time.time())}")
        try:
            path.rename(quarantine)
        except OSError as rn_exc:
            print(
                f"toolforge_local_scan: quarantine of {path} failed: {rn_exc}",
                file=sys.stderr,
            )
        return None
    except OSError as exc:
        # Not FileNotFoundError (caught by .exists() check above) — permission
        # or IO issue worth surfacing.
        print(
            f"toolforge_local_scan: cache read failed at {path}: {exc}",
            file=sys.stderr,
        )
        return None


def _save_cache(category: str, data: list[dict]) -> None:
    path = _cache_path(category)
    try:
        # WARN: see SKETCHY_CODE_AUDIT.md#s4-4 — FIXED in F11 (atomic write via tmp + os.replace).
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"toolforge_local_scan: cache write failed: {exc}", file=sys.stderr)


# ---------- section: public-api ----------
def scan(category: str, force: bool = False) -> list[dict]:
    if category not in SUPPORTED_CATEGORIES:
        raise ValueError(f"unsupported category {category!r}: pick one of {sorted(SUPPORTED_CATEGORIES)}")
    if not force:
        cached = _load_cache(category)
        if cached is not None:
            return cached
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    local = _scan_local_paths(category, deadline)
    installed = _enrich_installed(_scan_installed_plugins() + _scan_installed_mcps(), category)
    merged = _dedupe_and_cap(local + installed)
    _save_cache(category, merged)
    return merged


def rescan_all() -> int:
    cleared = 0
    for cat in SUPPORTED_CATEGORIES:
        p = _cache_path(cat)
        if p.exists():
            try:
                p.unlink()
                cleared += 1
            except OSError:
                pass
    return cleared


# ---------- section: self-test ----------
def _self_test() -> int:
    passed = 0
    failed = 0
    try:
        assert _normalize_name("Frontend-Design") == "frontend-design"
        # Canonical impl drops disallowed chars silently (see toolforge_usage_detector._normalize_name).
        assert _normalize_name("foo;bar") == "foobar"
        assert _normalize_name("foo:bar") == "foo/bar"  # colon -> slash for plugin:skill namespacing
        assert _normalize_name("") == ""
        print("OK: name normalization")
        passed += 1
    except AssertionError as exc:
        print(f"FAIL: name normalization: {exc}")
        failed += 1
    try:
        ui_score = _category_score("shadcn-ui-mcp", "shadcn React components MCP", Path("/x/y/ui/shadcn.md"), "ui")
        assert ui_score >= 0.5, f"shadcn UI scored {ui_score}, expected >= 0.5"
        backend_score = _category_score("postgres-mcp", "postgres database tools", Path("/x/y/postgres.md"), "ui")
        assert backend_score < MIN_CATEGORY_SCORE, f"postgres should not match ui, got {backend_score}"
        db_score = _category_score("postgres-mcp", "postgres database tools", Path("/x/y/postgres.md"), "database")
        assert db_score >= 0.5, f"postgres DB scored {db_score}, expected >= 0.5"
        print("OK: category scoring")
        passed += 1
    except AssertionError as exc:
        print(f"FAIL: category scoring: {exc}")
        failed += 1
    try:
        from datetime import datetime
        fm = _parse_frontmatter("---\nname: my-skill\ndescription: a test skill\n---\nbody")
        assert fm.get("name") == "my-skill"
        assert fm.get("description") == "a test skill"
        print("OK: frontmatter parser")
        passed += 1
    except AssertionError as exc:
        print(f"FAIL: frontmatter parser: {exc}")
        failed += 1
    try:
        assert scan.__name__ == "scan"
        try:
            scan("invalid_category")
            failed += 1
            print("FAIL: invalid category accepted")
        except ValueError:
            print("OK: invalid category rejected")
            passed += 1
    except AssertionError as exc:
        print(f"FAIL: scan signature: {exc}")
        failed += 1
    try:
        n = rescan_all()
        assert isinstance(n, int) and n >= 0
        print(f"OK: rescan_all cleared {n} caches")
        passed += 1
    except (AssertionError, OSError) as exc:
        print(f"FAIL: rescan_all: {exc}")
        failed += 1
    try:
        # WARN: see SKETCHY_CODE_AUDIT.md#s5-2 — FIXED in F39 (sprint tag dropped from local_scan.py).
        # stat() failure must return None (not 0.0) so downstream caller drops
        # the entry instead of silently ranking a broken scan as "an old tool".
        # Use a non-git path to skip the git-log branch and hit the stat fallback.
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
    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- section: cli-entry-point ----------
def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_local_scan.py scan <category> [--force]\n"
        "  toolforge_local_scan.py rescan-all\n"
        "  toolforge_local_scan.py --self-test\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help"}:
        print(_usage(), file=sys.stderr)
        return 2
    if argv[1] == "--self-test":
        return _self_test()
    if argv[1] == "rescan-all":
        n = rescan_all()
        print(f"cleared {n} cache file(s)")
        return 0
    if argv[1] == "scan":
        if len(argv) < 3:
            print(_usage(), file=sys.stderr)
            return 2
        category = argv[2].lower()
        force = "--force" in argv[3:]
        try:
            result = scan(category, force=force)
        except ValueError as exc:
            print(f"toolforge_local_scan error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        return 0
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
