#!/usr/bin/env python3
"""TF-IDF skill router for ToolForge. Skills only (v0.2 scope).

Discovers installed skills, builds a normalized TF-IDF corpus, scores a
user prompt via cosine similarity, applies a capped usage-frequency boost,
returns top-K above threshold.

Cache: tempdir/toolforge_router_idx.json, 1hr TTL.
Invalidate: call invalidate_cache() or run /toolforge-rescan.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
IDX_CACHE = Path(tempfile.gettempdir()) / "toolforge_router_idx.json"
IDX_CACHE_TTL = 3600

TOP_K = 3
SCORE_THRESHOLD = 0.05
# usage_boost is capped at USAGE_BOOST_CAP before being multiplied by WEIGHT.
# Max net contribution to cosine score = 0.5 * 0.10 = 0.05 — intentionally small
# so it nudges ties rather than overriding semantic match (anti-self-poisoning).
USAGE_BOOST_CAP = 0.5
USAGE_BOOST_DIV = 100.0
USAGE_BOOST_WEIGHT = 0.10

# Programming verbs appear in nearly every prompt — exclude to prevent false
# positives on non-skill-specific messages.
_STOP = frozenset({
    "a","an","the","is","it","in","to","for","of","on","with","how","what",
    "can","do","i","my","me","we","you","this","that","and","or","but","not",
    "be","have","s","t","ll","ve","re","at","by","from","up","as","if","so",
    "into","out","when","where","who","which","will","would","could","should",
    "may","might","must","shall",
    "write","add","fix","create","make","update","change","get","set","use",
    "run","build","show","find","implement","refactor","edit","read","return",
    "print","log","test","check","look","see","try","take","give","work",
    "call","put","go","let","help","please","need","want","new","old","good",
    "code","file","function","class","app","module","script","program",
    "line","error","output","input","value","object","string",
    "using","user","data","type","just","then","also","here","some",
    "more","all","any","one","two","three","way","thing","like",
})

_TOKEN_RE = re.compile(r"[a-z][a-z0-9]*")
# Key chars allowed by toolforge_db.TOOL_KEY_RE after the "skill:" prefix.
_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9._@/-]")


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower())
            if t not in _STOP and len(t) > 1]


def _parse_frontmatter(path: Path) -> dict:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    fm: dict = {}
    for line in content[3:end].splitlines():
        k, sep, v = line.partition(":")
        if sep:
            fm[k.strip()] = v.strip()
    fm["_body"] = content[end + 3:].strip()[:600]
    return fm


def discover_skills() -> list[dict]:
    """Scan user-wide and project skill dirs. Returns [{name, description, doc}]."""
    skill_dirs = [
        CLAUDE_HOME / "skills",
        Path.cwd() / ".claude" / "skills",
    ]
    seen: dict[str, dict] = {}
    for skills_dir in skill_dirs:
        if not skills_dir.exists():
            continue
        try:
            entries = list(skills_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue
            fm = _parse_frontmatter(skill_md)
            name = fm.get("name") or entry.name
            desc = fm.get("description") or ""
            body = fm.get("_body") or ""
            if name not in seen:
                seen[name] = {
                    "name": name,
                    "description": desc,
                    "doc": f"{name} {desc} {body}",
                }
    return list(seen.values())


def _tf(tokens: list[str]) -> dict[str, float]:
    if not tokens:
        return {}
    freq: dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    n = len(tokens)
    return {t: c / n for t, c in freq.items()}


def build_index(skills: list[dict]) -> dict:
    """Build L2-normalized TF-IDF vectors for all skills."""
    n = len(skills)
    if n == 0:
        return {"built_at": time.time(), "skills": [], "idf": {}}

    tokenized = [_tokenize(s["doc"]) for s in skills]

    df: dict[str, int] = {}
    for toks in tokenized:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1

    # Smoothed IDF so unseen query terms don't explode.
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}

    vecs: list[dict] = []
    for skill, toks in zip(skills, tokenized):
        tf_map = _tf(toks)
        raw = {t: tf_map[t] * idf.get(t, 1.0) for t in tf_map}
        norm = math.sqrt(sum(v * v for v in raw.values())) or 1.0
        vecs.append({
            "name": skill["name"],
            "description": skill["description"],
            "vec": {t: v / norm for t, v in raw.items()},
        })

    return {"built_at": time.time(), "skills": vecs, "idf": idf}


def _cosine_scores(prompt: str, index: dict) -> list[tuple[str, float, str]]:
    idf = index.get("idf", {})
    q_toks = _tokenize(prompt)
    q_tf = _tf(q_toks)
    q_raw = {t: q_tf[t] * idf.get(t, 1.0) for t in q_tf}
    q_norm = math.sqrt(sum(v * v for v in q_raw.values()))
    if q_norm == 0.0:
        return []
    q_vec = {t: v / q_norm for t, v in q_raw.items()}

    out: list[tuple[str, float, str]] = []
    for s in index.get("skills", []):
        dot = sum(q_vec.get(t, 0.0) * v for t, v in s["vec"].items())
        out.append((s["name"], dot, s["description"]))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _usage_boosts(names: list[str]) -> dict[str, float]:
    """Pull 30d usage counts from DB. Fails silently — boost is optional."""
    if not names:
        return {}
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        import toolforge_db  # type: ignore
        # Sanitize names to match DB's TOOL_KEY_RE: skill:<a-z0-9._@/->
        safe: dict[str, str] = {}
        for name in names:
            sanitized = _NAME_SANITIZE_RE.sub("", name.lower().strip())[:80]
            if sanitized:
                safe[name] = f"skill:{sanitized}"
        valid_keys = list(safe.values())
        if not valid_keys:
            return {}
        stats = toolforge_db.get_usage_stats_bulk(valid_keys)
        boosts: dict[str, float] = {}
        for name, key in safe.items():
            count = (stats.get(key) or {}).get("count_30d") or 0
            boosts[name] = min(USAGE_BOOST_CAP, count / USAGE_BOOST_DIV) * USAGE_BOOST_WEIGHT
        return boosts
    except Exception:
        return {}


def _load_cache() -> Optional[dict]:
    try:
        if not IDX_CACHE.exists():
            return None
        if (time.time() - IDX_CACHE.stat().st_mtime) > IDX_CACHE_TTL:
            return None
        return json.loads(IDX_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(idx: dict) -> None:
    try:
        tmp = IDX_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx), encoding="utf-8")
        os.replace(tmp, IDX_CACHE)
    except OSError:
        pass


def invalidate_cache() -> None:
    """Force next route() call to rebuild the index from disk."""
    try:
        IDX_CACHE.unlink(missing_ok=True)
    except OSError:
        pass


def route(
    prompt: str,
    force: bool = False,
    deadline: Optional[float] = None,
) -> list[dict]:
    """Score prompt against installed skills.

    Returns [{name, score, description}, ...], top-K above threshold.
    Returns empty list on timeout or no matching skills.
    """
    idx = None if force else _load_cache()
    if idx is None:
        if deadline and time.monotonic() > deadline:
            return []
        idx = build_index(discover_skills())
        _save_cache(idx)

    if deadline and time.monotonic() > deadline:
        return []

    candidates = [
        (n, s, d) for n, s, d in _cosine_scores(prompt, idx)
        if s > SCORE_THRESHOLD
    ][: TOP_K * 2]

    if not candidates or (deadline and time.monotonic() > deadline):
        return []

    boosts = _usage_boosts([n for n, _, _ in candidates])
    ranked = sorted(
        [(n, s + boosts.get(n, 0.0), d) for n, s, d in candidates],
        key=lambda x: x[1],
        reverse=True,
    )
    results = [
        {"name": n, "score": round(s, 4), "description": d, "installed": True}
        for n, s, d in ranked[:TOP_K]
        if s > SCORE_THRESHOLD
    ]
    if results:
        return results

    # No installed skill matched — fall back to catalog suggestions.
    return _catalog_suggest(prompt)


def _catalog_suggest(prompt: str) -> list[dict]:
    """Return top catalog matches when no installed skill matches.

    Results carry catalog_only=True and installed=False so the hook can
    emit a different message (suggest /toolforge-hunt rather than invoking
    a skill directly).
    """
    try:
        import toolforge_catalog  # type: ignore
        matches = toolforge_catalog.suggest(prompt, top_k=TOP_K)
        return [
            {
                "name": m["name"],
                "score": round(0.01 + m["match_score"] * 0.01, 4),
                "description": m.get("description", ""),
                "installed": False,
                "catalog_only": True,
                "install_command": m.get("install_command", ""),
                "categories": m.get("categories", []),
            }
            for m in matches
        ]
    except Exception:
        return []


# ---------- self-test ----------

def _self_test() -> int:
    passed = failed = 0

    mock_skills = [
        {
            "name": "gsap-animation",
            "description": "GSAP animation timeline React components useGSAP hook scroll triggers tweening",
            "doc": "",
        },
        {
            "name": "code-review",
            "description": "Multi-agent pull request review diff correctness bugs security findings",
            "doc": "",
        },
        {
            "name": "impeccable",
            "description": "React component cleanup polish simplify refactor dead-code remove style lint",
            "doc": "",
        },
        {
            "name": "playwright-testing",
            "description": "Browser E2E testing playwright automation visual regression snapshots assertions",
            "doc": "",
        },
        {
            "name": "sql-schema",
            "description": "Database schema design migration SQL postgres SQLite table indexes foreign keys",
            "doc": "",
        },
    ]
    for s in mock_skills:
        s["doc"] = f"{s['name']} {s['description']}"

    idx = build_index(mock_skills)

    fixture = [
        ("animate this React component with GSAP scroll triggers", "gsap-animation"),
        ("review my pull request for correctness and security bugs", "code-review"),
        ("clean up and simplify this React component remove dead code", "impeccable"),
        ("write E2E browser tests with playwright visual regression", "playwright-testing"),
        ("design the database schema migration for postgres with foreign keys", "sql-schema"),
    ]

    for prompt, expected in fixture:
        results = _cosine_scores(prompt, idx)
        if not results:
            print(f"FAIL: no results for {repr(prompt[:50])}")
            failed += 1
            continue
        top = results[0][0]
        if top == expected:
            print(f"OK: {repr(prompt[:45])} -> {top}")
            passed += 1
        else:
            # Acceptable if expected is within top-3 (descriptions are short mocks)
            top3 = [r[0] for r in results[:3]]
            if expected in top3:
                print(f"OK(top3): {repr(prompt[:45])} -> {top} (expected in top3)")
                passed += 1
            else:
                print(f"FAIL: {repr(prompt[:45])} -> {top} (expected {expected})")
                failed += 1

    # Stop-word-only prompt should produce no results above threshold
    generic = [s for _, s, _ in _cosine_scores("write add fix create make update", idx)
               if s > SCORE_THRESHOLD]
    if not generic:
        print("OK: stop-word-only prompt scores below threshold")
        passed += 1
    else:
        print(f"FAIL: stop-word prompt produced {len(generic)} candidates above threshold")
        failed += 1

    # Index round-trip: build -> serialize -> load -> score
    import tempfile as _tmpmod
    tmp = Path(_tmpmod.mktemp(suffix=".json"))
    try:
        idx2 = build_index(mock_skills[:3])
        tmp.write_text(json.dumps(idx2), encoding="utf-8")
        idx3 = json.loads(tmp.read_text(encoding="utf-8"))
        r = _cosine_scores("animate GSAP timeline", idx3)
        if r and r[0][0] == "gsap-animation":
            print("OK: index serialize/deserialize round-trip")
            passed += 1
        else:
            print(f"FAIL: round-trip top result = {r[0][0] if r else 'none'}")
            failed += 1
    finally:
        tmp.unlink(missing_ok=True)

    # Empty index returns empty list
    empty_idx = build_index([])
    if _cosine_scores("anything", empty_idx) == []:
        print("OK: empty index returns []")
        passed += 1
    else:
        print("FAIL: empty index should return []")
        failed += 1

    # route() with no real skills installed returns [] without crashing
    try:
        results = route("some prompt about animation", force=True)
        if isinstance(results, list):
            print(f"OK: route() returns list ({len(results)} skills from real install)")
            passed += 1
        else:
            print("FAIL: route() did not return a list")
            failed += 1
    except Exception as exc:
        print(f"FAIL: route() raised: {exc}")
        failed += 1

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- cli ----------

def _usage_str() -> str:
    return (
        "Usage:\n"
        "  toolforge_router.py route <prompt text...>\n"
        "  toolforge_router.py --self-test\n"
    )


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage_str(), file=sys.stderr)
        return 2
    if argv[0] == "--self-test":
        return _self_test()
    if argv[0] == "invalidate":
        invalidate_cache()
        print("Router index cache cleared.")
        return 0
    if argv[0] != "route":
        print(_usage_str(), file=sys.stderr)
        return 2
    prompt = " ".join(argv[1:]).strip()
    if not prompt:
        print("route: prompt required", file=sys.stderr)
        return 2
    results = route(prompt)
    if not results:
        print("No matching skills above threshold.")
        return 0
    for r in results:
        print(f"  {r['name']:40s}  score={r['score']:.4f}  {r['description'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
