#!/usr/bin/env python3
"""ToolForge declarative stack detector.

Powers bare `/toolforge` (no category argument): scans a repo's manifests
(root + depth<=2 only) and maps what it finds onto ToolForge categories so the
command can recommend where to start instead of demanding a hand-typed
category. Detection is declarative — RULES maps package names / config files /
file globs to a tech id plus categories, COMBO_RULES boosts a category when a
known pairing (e.g. nextjs + supabase) is present. Rule shape ported from
autoskills' SKILLS_MAP / COMBO_SKILLS_MAP (skills-map.ts:
detect.packages/configFiles/fileExtensions -> packages/config_files/file_globs,
ComboSkill.requires -> COMBO_RULES requires).

Output schema (consumed by commands/toolforge.md Step 0 and the curator's
stack-match bonus, skills/toolforge-curator/SKILL.md section 6):

  {
    "technologies":      [{"tech", "name", "categories", "evidence"}],
    "ranked_categories": [{"category", "score"}],
    "combos":            [{"requires", "boost_category"}]
  }

Category score = matched-tech count normalized to 0-1, +0.2 per fired combo
boost for that category, capped at 1.0.

Detection inputs: package.json deps+devDependencies, requirements.txt,
pyproject.toml, go.mod, Cargo.toml, docker-compose.yml image names,
.github/workflows existence, config-file existence.

Cache: tempdir/toolforge_stack_detect_<sha1(abspath)[:16]>.json, 5-min TTL.
Pass --force to bypass. Corrupt cache files are quarantined, never trusted.

Security boundaries:
  - No symlink follow.
  - Per-file read cap: 256 KiB (manifests only; nothing else is opened).
  - Hard cap on files scanned: 500.
  - Depth cap: 2 below the scan root.
  - No subprocess, no network. Pure-stdlib manifest reads.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# tomllib is stdlib on 3.11+; the README support floor is 3.10. Fall back to the
# tomli backport if present, else degrade to None so TOML manifests are skipped
# (return []) rather than crashing the whole detector at import on 3.10.
try:
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None
_TOMLDecodeError = getattr(_toml, "TOMLDecodeError", Exception)

# ---------- section: constants ----------
CACHE_TTL_SECONDS = 300
SCAN_BUDGET_SECONDS = 5.0
MAX_FILES_SCANNED = 500
MAX_FILE_READ_BYTES = 262144
MAX_DEPTH = 2
COMBO_BOOST = 0.2
MAX_EVIDENCE_PER_TECH = 5

SUPPORTED_CATEGORIES = ("ui", "backend", "database", "testing", "devops")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", ".vite", ".venv", "venv"}

# ---------- section: rules ----------
# Keys: tech (stable id), name (display), categories (subset of
# SUPPORTED_CATEGORIES), packages (manifest dependency names, exact
# case-insensitive match), config_files (relative path existence, any depth<=2
# suffix match), file_globs (basename fnmatch).
RULES: list[dict] = [
    # -- ui --
    {"tech": "react", "name": "React", "categories": ["ui"],
     "packages": ["react", "react-dom"], "config_files": [], "file_globs": []},
    {"tech": "nextjs", "name": "Next.js", "categories": ["ui"],
     "packages": ["next"],
     "config_files": ["next.config.js", "next.config.mjs", "next.config.ts"],
     "file_globs": []},
    {"tech": "vue", "name": "Vue", "categories": ["ui"],
     "packages": ["vue"], "config_files": [], "file_globs": ["*.vue"]},
    {"tech": "svelte", "name": "Svelte", "categories": ["ui"],
     "packages": ["svelte", "@sveltejs/kit"],
     "config_files": ["svelte.config.js"], "file_globs": ["*.svelte"]},
    {"tech": "angular", "name": "Angular", "categories": ["ui"],
     "packages": ["@angular/core"], "config_files": ["angular.json"], "file_globs": []},
    {"tech": "astro", "name": "Astro", "categories": ["ui"],
     "packages": ["astro"],
     "config_files": ["astro.config.mjs", "astro.config.js", "astro.config.ts"],
     "file_globs": ["*.astro"]},
    {"tech": "tailwind", "name": "Tailwind CSS", "categories": ["ui"],
     "packages": ["tailwindcss", "@tailwindcss/vite", "@tailwindcss/postcss"],
     "config_files": ["tailwind.config.js", "tailwind.config.ts", "tailwind.config.cjs"],
     "file_globs": []},
    {"tech": "gsap", "name": "GSAP", "categories": ["ui"],
     "packages": ["gsap", "@gsap/react"], "config_files": [], "file_globs": []},
    # -- backend --
    {"tech": "express", "name": "Express", "categories": ["backend"],
     "packages": ["express"], "config_files": [], "file_globs": []},
    {"tech": "fastapi", "name": "FastAPI", "categories": ["backend"],
     "packages": ["fastapi"], "config_files": [], "file_globs": []},
    {"tech": "django", "name": "Django", "categories": ["backend"],
     "packages": ["django", "djangorestframework"],
     "config_files": ["manage.py"], "file_globs": []},
    {"tech": "flask", "name": "Flask", "categories": ["backend"],
     "packages": ["flask"], "config_files": [], "file_globs": []},
    {"tech": "nestjs", "name": "NestJS", "categories": ["backend"],
     "packages": ["@nestjs/core", "@nestjs/common"],
     "config_files": ["nest-cli.json"], "file_globs": []},
    {"tech": "go", "name": "Go", "categories": ["backend"],
     "packages": [], "config_files": ["go.mod"], "file_globs": ["*.go"]},
    {"tech": "rust", "name": "Rust", "categories": ["backend"],
     "packages": [], "config_files": ["Cargo.toml"], "file_globs": ["*.rs"]},
    # -- database --
    {"tech": "prisma", "name": "Prisma", "categories": ["database"],
     "packages": ["prisma", "@prisma/client"],
     "config_files": ["prisma/schema.prisma", "schema.prisma"], "file_globs": []},
    {"tech": "supabase", "name": "Supabase", "categories": ["database"],
     "packages": ["@supabase/supabase-js", "@supabase/ssr", "supabase"],
     "config_files": ["supabase/config.toml"], "file_globs": []},
    {"tech": "postgres", "name": "PostgreSQL", "categories": ["database"],
     "packages": ["pg", "postgres", "psycopg", "psycopg2", "psycopg2-binary", "asyncpg"],
     "config_files": [], "file_globs": []},
    {"tech": "sqlite", "name": "SQLite", "categories": ["database"],
     "packages": ["better-sqlite3", "sqlite3", "aiosqlite"],
     "config_files": [], "file_globs": ["*.sqlite", "*.sqlite3"]},
    {"tech": "mongodb", "name": "MongoDB", "categories": ["database"],
     "packages": ["mongodb", "mongoose", "pymongo", "motor"],
     "config_files": [], "file_globs": []},
    {"tech": "drizzle", "name": "Drizzle ORM", "categories": ["database"],
     "packages": ["drizzle-orm", "drizzle-kit"],
     "config_files": ["drizzle.config.ts", "drizzle.config.js"], "file_globs": []},
    {"tech": "redis", "name": "Redis", "categories": ["database"],
     "packages": ["redis", "ioredis", "@upstash/redis"],
     "config_files": [], "file_globs": []},
    {"tech": "firebase", "name": "Firebase", "categories": ["database"],
     "packages": ["firebase", "firebase-admin", "firebase-functions"],
     "config_files": ["firebase.json", ".firebaserc"], "file_globs": []},
    # -- testing --
    {"tech": "jest", "name": "Jest", "categories": ["testing"],
     "packages": ["jest", "ts-jest"],
     "config_files": ["jest.config.js", "jest.config.ts", "jest.config.mjs"],
     "file_globs": []},
    {"tech": "vitest", "name": "Vitest", "categories": ["testing"],
     "packages": ["vitest"],
     "config_files": ["vitest.config.ts", "vitest.config.js", "vitest.config.mjs"],
     "file_globs": []},
    {"tech": "pytest", "name": "pytest", "categories": ["testing"],
     "packages": ["pytest"],
     "config_files": ["pytest.ini", "conftest.py"], "file_globs": []},
    {"tech": "playwright", "name": "Playwright", "categories": ["testing"],
     "packages": ["@playwright/test", "playwright", "pytest-playwright"],
     "config_files": ["playwright.config.ts", "playwright.config.js"], "file_globs": []},
    {"tech": "cypress", "name": "Cypress", "categories": ["testing"],
     "packages": ["cypress"],
     "config_files": ["cypress.config.js", "cypress.config.ts"], "file_globs": []},
    # -- devops --
    {"tech": "docker", "name": "Docker", "categories": ["devops"],
     "packages": [],
     "config_files": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml", ".dockerignore"],
     "file_globs": []},
    {"tech": "kubernetes", "name": "Kubernetes", "categories": ["devops"],
     "packages": ["kubernetes"],
     "config_files": ["Chart.yaml", "kustomization.yaml", "skaffold.yaml"],
     "file_globs": []},
    {"tech": "terraform", "name": "Terraform", "categories": ["devops"],
     "packages": [], "config_files": [".terraform.lock.hcl"], "file_globs": ["*.tf"]},
    {"tech": "github-actions", "name": "GitHub Actions", "categories": ["devops"],
     "packages": [], "config_files": [".github/workflows"], "file_globs": []},
]

COMBO_RULES: list[dict] = [
    {"requires": ["nextjs", "supabase"], "boost_category": "database"},
    {"requires": ["react", "tailwind"], "boost_category": "ui"},
    {"requires": ["gsap", "react"], "boost_category": "ui"},
    {"requires": ["nextjs", "playwright"], "boost_category": "testing"},
    {"requires": ["docker", "kubernetes"], "boost_category": "devops"},
    {"requires": ["prisma", "postgres"], "boost_category": "database"},
]


# ---------- section: manifest-parsers ----------
def _read_capped(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_FILE_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


_REQUIREMENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_GO_REQUIRE_RE = re.compile(r"^\s*(?:require\s+)?([A-Za-z0-9._~/-]+)\s+v\d", re.MULTILINE)
_COMPOSE_IMAGE_RE = re.compile(r"^\s*image:\s*[\"']?([A-Za-z0-9._/:-]+)", re.MULTILINE)


def _parse_package_json(path: Path) -> list[str]:
    text = _read_capped(path)
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"toolforge_stack_detect: unparseable {path}: {exc}", file=sys.stderr)
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key)
        if isinstance(deps, dict):
            out.extend(str(k) for k in deps)
    return out


def _parse_requirements_txt(path: Path) -> list[str]:
    out: list[str] = []
    for line in _read_capped(path).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = _REQUIREMENT_NAME_RE.match(line)
        if m:
            out.append(m.group(0))
    return out


def _parse_pyproject_toml(path: Path) -> list[str]:
    if _toml is None:
        return []
    text = _read_capped(path)
    if not text:
        return []
    try:
        data = _toml.loads(text)
    except _TOMLDecodeError as exc:
        print(f"toolforge_stack_detect: unparseable {path}: {exc}", file=sys.stderr)
        return []
    out: list[str] = []
    project = data.get("project") or {}
    reqs = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        reqs.extend(group or [])
    for req in reqs:
        m = _REQUIREMENT_NAME_RE.match(str(req).strip())
        if m:
            out.append(m.group(0))
    poetry_deps = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies")
    if isinstance(poetry_deps, dict):
        out.extend(k for k in poetry_deps if k.lower() != "python")
    return out


def _parse_go_mod(path: Path) -> list[str]:
    out: list[str] = []
    for mod in _GO_REQUIRE_RE.findall(_read_capped(path)):
        out.append(mod)
        out.append(mod.rsplit("/", 1)[-1])
    return out


def _parse_cargo_toml(path: Path) -> list[str]:
    if _toml is None:
        return []
    text = _read_capped(path)
    if not text:
        return []
    try:
        data = _toml.loads(text)
    except _TOMLDecodeError as exc:
        print(f"toolforge_stack_detect: unparseable {path}: {exc}", file=sys.stderr)
        return []
    out: list[str] = []
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        deps = data.get(key)
        if isinstance(deps, dict):
            out.extend(deps)
    workspace_deps = (data.get("workspace") or {}).get("dependencies")
    if isinstance(workspace_deps, dict):
        out.extend(workspace_deps)
    return out


def _parse_docker_compose(path: Path) -> list[str]:
    out: list[str] = []
    for image in _COMPOSE_IMAGE_RE.findall(_read_capped(path)):
        # `postgres:16` / `bitnami/redis:7` -> bare image name matches package rules.
        out.append(image.split(":")[0].rsplit("/", 1)[-1])
    return out


MANIFEST_PARSERS = {
    "package.json": _parse_package_json,
    "requirements.txt": _parse_requirements_txt,
    "pyproject.toml": _parse_pyproject_toml,
    "go.mod": _parse_go_mod,
    "cargo.toml": _parse_cargo_toml,
    "docker-compose.yml": _parse_docker_compose,
    "docker-compose.yaml": _parse_docker_compose,
}


# ---------- section: inventory ----------
def _collect_inventory(root: Path, deadline: float) -> dict:
    """Walk root with depth + count + deadline caps. No symlink follow.

    Returns packages (dep name -> source manifest rel path, first hit wins),
    files (rel posix paths), dirs (rel posix paths).
    """
    packages: dict[str, str] = {}
    files: set[str] = set()
    dirs: set[str] = set()
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
                    if count >= MAX_FILES_SCANNED:
                        print(f"toolforge_stack_detect: file-count cap reached at {MAX_FILES_SCANNED}", file=sys.stderr)
                        break
                    count += 1
                    p = Path(entry.path)
                    rel = p.relative_to(root).as_posix()
                    if entry.is_dir(follow_symlinks=False):
                        dirs.add(rel)
                        if entry.name in SKIP_DIRS:
                            continue
                        stack.append((p, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        files.add(rel)
                        parser = MANIFEST_PARSERS.get(entry.name.lower())
                        if parser:
                            for pkg in parser(p):
                                packages.setdefault(pkg.lower(), rel)
        except OSError as exc:
            print(f"toolforge_stack_detect: unreadable subdir {current!r}: {exc}", file=sys.stderr)
            continue
    return {"packages": packages, "files": files, "dirs": dirs}


# ---------- section: rule-matching ----------
def _find_config(cfg: str, inventory: dict) -> Optional[str]:
    pool = inventory["files"] | inventory["dirs"]
    if cfg in pool:
        return cfg
    suffix = "/" + cfg
    # Monorepo layouts: packages/web/next.config.ts still counts.
    for rel in sorted(pool):
        if rel.endswith(suffix):
            return rel
    return None


def _match_rule(rule: dict, inventory: dict) -> list[str]:
    evidence: list[str] = []
    for pkg in rule["packages"]:
        src = inventory["packages"].get(pkg.lower())
        if src is not None:
            evidence.append(f"{src}: {pkg}")
    for cfg in rule["config_files"]:
        hit = _find_config(cfg, inventory)
        if hit:
            evidence.append(f"config: {hit}")
    for pattern in rule["file_globs"]:
        hit = next(
            (f for f in sorted(inventory["files"]) if fnmatch.fnmatch(f.rsplit("/", 1)[-1], pattern)),
            None,
        )
        if hit:
            evidence.append(f"file: {hit}")
    return evidence[:MAX_EVIDENCE_PER_TECH]


# ---------- section: scoring ----------
def _rank_categories(technologies: list[dict], combos: list[dict]) -> list[dict]:
    counts: dict[str, int] = {c: 0 for c in SUPPORTED_CATEGORIES}
    for tech in technologies:
        for cat in tech["categories"]:
            if cat in counts:
                counts[cat] += 1
    max_count = max(counts.values(), default=0)
    if max_count == 0:
        return []
    scores: dict[str, float] = {}
    for cat, n in counts.items():
        if n > 0:
            scores[cat] = n / max_count
    for combo in combos:
        cat = combo["boost_category"]
        scores[cat] = scores.get(cat, 0.0) + COMBO_BOOST
    ranked = [
        {"category": cat, "score": round(min(1.0, score), 3)}
        for cat, score in scores.items()
    ]
    ranked.sort(key=lambda r: (-r["score"], r["category"]))
    return ranked


# ---------- section: cache ----------
def _cache_path(root: Path) -> Path:
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"toolforge_stack_detect_{digest}.json"


def _load_cache(root: Path) -> Optional[dict]:
    path = _cache_path(root)
    if not path.exists():
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if (time.time() - st.st_mtime) > CACHE_TTL_SECONDS:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Corruption (partial write, disk error, tampering). Quarantine instead
        # of unlink so forensic evidence survives. Re-scan on return.
        print(
            f"toolforge_stack_detect: cache corrupt at {path}, re-scanning: "
            f"{exc.msg} at line {exc.lineno} col {exc.colno}",
            file=sys.stderr,
        )
        quarantine = path.with_suffix(path.suffix + f".corrupt.{int(time.time())}")
        try:
            path.rename(quarantine)
        except OSError as rn_exc:
            print(f"toolforge_stack_detect: quarantine of {path} failed: {rn_exc}", file=sys.stderr)
        return None
    except OSError as exc:
        print(f"toolforge_stack_detect: cache read failed at {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or "technologies" not in data:
        print(f"toolforge_stack_detect: cache schema mismatch at {path}, re-scanning", file=sys.stderr)
        return None
    return data


def _save_cache(root: Path, data: dict) -> None:
    path = _cache_path(root)
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"toolforge_stack_detect: cache write failed: {exc}", file=sys.stderr)


# ---------- section: public-api ----------
def detect(path: str, force: bool = False) -> dict:
    root = Path(os.path.expanduser(path))
    try:
        root = root.resolve()
    except OSError as exc:
        raise ValueError(f"cannot resolve path {path!r}: {exc}") from exc
    if not root.is_dir():
        raise ValueError(f"not a directory: {path!r}")
    if not force:
        cached = _load_cache(root)
        if cached is not None:
            return cached
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    inventory = _collect_inventory(root, deadline)
    technologies: list[dict] = []
    for rule in RULES:
        evidence = _match_rule(rule, inventory)
        if evidence:
            technologies.append({
                "tech": rule["tech"],
                "name": rule["name"],
                "categories": list(rule["categories"]),
                "evidence": evidence,
            })
    matched_ids = {t["tech"] for t in technologies}
    combos = [
        {"requires": list(c["requires"]), "boost_category": c["boost_category"]}
        for c in COMBO_RULES
        if all(r in matched_ids for r in c["requires"])
    ]
    result = {
        "technologies": technologies,
        "ranked_categories": _rank_categories(technologies, combos),
        "combos": combos,
    }
    _save_cache(root, result)
    return result


# ---------- section: self-test ----------
def _self_test() -> int:
    passed = 0
    failed = 0
    try:
        seen_ids: set[str] = set()
        for rule in RULES:
            assert rule["tech"] not in seen_ids, f"duplicate tech id {rule['tech']}"
            seen_ids.add(rule["tech"])
            bad = set(rule["categories"]) - set(SUPPORTED_CATEGORIES)
            assert not bad, f"{rule['tech']} has unknown categories {bad}"
        for combo in COMBO_RULES:
            assert combo["boost_category"] in SUPPORTED_CATEGORIES, combo
            unknown = set(combo["requires"]) - seen_ids
            assert not unknown, f"combo references unknown techs {unknown}"
        print("OK: rule table integrity")
        passed += 1
    except AssertionError as exc:
        print(f"FAIL: rule table integrity: {exc}")
        failed += 1

    fixture = Path(tempfile.mkdtemp(prefix="toolforge_stack_fixture_"))
    empty_dir = Path(tempfile.mkdtemp(prefix="toolforge_stack_empty_"))
    result: Optional[dict] = None
    try:
        try:
            (fixture / "package.json").write_text(
                json.dumps({
                    "dependencies": {"next": "15.1.0", "@supabase/supabase-js": "2.45.0"},
                    "devDependencies": {},
                }),
                encoding="utf-8",
            )
            result = detect(str(fixture), force=True)
            techs = {t["tech"] for t in result["technologies"]}
            assert "nextjs" in techs, f"nextjs not detected, got {techs}"
            assert "supabase" in techs, f"supabase not detected, got {techs}"
            ranked = [r["category"] for r in result["ranked_categories"]]
            assert "database" in ranked, f"database not ranked, got {ranked}"
            assert "ui" in ranked, f"ui not ranked, got {ranked}"
            assert any(c["boost_category"] == "database" for c in result["combos"]), \
                f"nextjs+supabase combo did not fire: {result['combos']}"
            assert all(0.0 < r["score"] <= 1.0 for r in result["ranked_categories"]), \
                f"score out of bounds: {result['ranked_categories']}"
            print("OK: nextjs+supabase fixture detection")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL: fixture detection: {exc}")
            failed += 1
        try:
            assert result is not None, "fixture detection did not produce a result"
            # Manifest deleted: a fresh scan would find nothing, so an equal
            # result proves the cache was served.
            (fixture / "package.json").unlink()
            cached = detect(str(fixture), force=False)
            assert cached == result, "second call did not serve the cache"
            fresh = detect(str(fixture), force=True)
            assert fresh["technologies"] == [], f"--force did not bypass cache: {fresh['technologies']}"
            print("OK: cache hit on second call, --force bypasses")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL: cache behavior: {exc}")
            failed += 1
        try:
            empty_result = detect(str(empty_dir), force=True)
            assert empty_result == {"technologies": [], "ranked_categories": [], "combos": []}, \
                f"empty dir not empty: {empty_result}"
            print("OK: empty dir yields empty result")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL: empty dir: {exc}")
            failed += 1
        try:
            # pyproject parsing degrades to [] without a TOML reader (3.10, no
            # tomli backport) and parses normally with one (3.11+) — neither path
            # may raise.
            toml_dir = Path(tempfile.mkdtemp(prefix="toolforge_stack_toml_"))
            try:
                (toml_dir / "pyproject.toml").write_text(
                    '[project]\ndependencies = ["django>=5.0"]\n', encoding="utf-8"
                )
                deps = _parse_pyproject_toml(toml_dir / "pyproject.toml")
                if _toml is None:
                    assert deps == [], f"expected graceful skip without TOML reader, got {deps}"
                else:
                    assert "django" in deps, f"django not parsed from pyproject: {deps}"
                print("OK: pyproject toml parse/degrade")
                passed += 1
            finally:
                shutil.rmtree(toml_dir, ignore_errors=True)
        except AssertionError as exc:
            print(f"FAIL: pyproject toml parse/degrade: {exc}")
            failed += 1
    finally:
        for d in (fixture, empty_dir):
            shutil.rmtree(d, ignore_errors=True)
            try:
                _cache_path(d.resolve()).unlink(missing_ok=True)
            except OSError:
                pass
    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- section: cli-entry-point ----------
def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_stack_detect.py detect [path] [--force] [--json]\n"
        "  toolforge_stack_detect.py --self-test\n"
    )


def _print_human(result: dict) -> None:
    technologies = result["technologies"]
    if not technologies:
        print("No known technologies detected.")
        return
    print(f"Detected {len(technologies)} technologies:")
    for t in technologies:
        print(f"  {t['tech']:<16} {t['name']} -> {', '.join(t['categories'])}  [{t['evidence'][0]}]")
    print("Ranked categories:")
    for i, r in enumerate(result["ranked_categories"], 1):
        print(f"  {i}. {r['category']} (score {r['score']:.2f})")
    for combo in result["combos"]:
        print(f"  combo: {'+'.join(combo['requires'])} boosts {combo['boost_category']}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help"}:
        print(_usage(), file=sys.stderr)
        return 2
    if argv[1] == "--self-test":
        return _self_test()
    if argv[1] == "detect":
        rest = argv[2:]
        flags = {a for a in rest if a.startswith("--")}
        unknown = flags - {"--force", "--json"}
        positional = [a for a in rest if not a.startswith("--")]
        if unknown or len(positional) > 1:
            print(_usage(), file=sys.stderr)
            return 2
        path = positional[0] if positional else "."
        try:
            result = detect(path, force="--force" in flags)
        except ValueError as exc:
            print(f"toolforge_stack_detect error: {exc}", file=sys.stderr)
            return 2
        if "--json" in flags:
            print(json.dumps(result, indent=2))
        else:
            _print_human(result)
        return 0
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
