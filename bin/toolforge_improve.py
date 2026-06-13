#!/usr/bin/env python3
"""ToolForge improve loop. Finds the worst Bayesian-rated local skill, packages
it for a Claude rewrite, snapshots the original into SQLite before any edit,
and later promotes or reverts based on fresh Likert ratings vs the stored
baseline.

Ports EvoSkill's proposer step, outcome ledger (discarded ideas are surfaced so
they are never re-proposed), and accept/discard gate — scored by ToolForge's
existing rating pipeline instead of benchmark runs.

Subcommands:
  candidates  — skills whose shrunk decayed score is below threshold
  package     — JSON bundle (content + rating history + prior-attempt ledger)
  commit      — snapshot original to skill_versions, then atomic-write rewrite
  verdict     — PROMOTE / REVERT / PENDING from post-rewrite ratings
  rollback    — atomic-restore the latest pending backup

CLI exit codes: 0 success, 1 no data (no candidates / no pending version),
2 usage error, 3 sqlite/os error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).parent
sys.path.insert(0, str(THIS_DIR))
import toolforge_db as db

DEFAULT_THRESHOLD = 2.8
DEFAULT_MIN_N = 3
# EvoSkill-ladder truncation: keep the head (frontmatter + core rules) and the
# tail (examples / recent additions), drop the middle when oversized.
HEAD_CAP = 16 * 1024
TAIL_CAP = 4 * 1024
NEW_MD_CAP = 64 * 1024
MIN_VERDICT_RATINGS = 3
RATING_HISTORY_LIMIT = 50

# Swapped by _self_test so skill resolution never touches real skill dirs.
_ROOTS_OVERRIDE: Optional[list[Path]] = None


# ---------- section: skill-resolution ----------


def _skill_roots() -> list[Path]:
    if _ROOTS_OVERRIDE is not None:
        return _ROOTS_OVERRIDE
    return [
        Path(os.path.expanduser("~/.claude/skills")),
        Path.cwd() / ".claude" / "skills",
    ]


def _skill_md_path(skill_name: str) -> Optional[Path]:
    """Resolve <name> to a SKILL.md under user or project skills.

    Symlinked skill dirs/files are skipped: a rewrite through a symlink would
    mutate a file outside the skill tree the user thinks they're editing.
    """
    for root in _skill_roots():
        skill_dir = root / skill_name
        cand = skill_dir / "SKILL.md"
        if skill_dir.is_symlink() or cand.is_symlink():
            continue
        if cand.is_file():
            return cand
    return None


def _atomic_write_text(path: Path, content: str) -> None:
    # Per FIX_CONVENTIONS.md §1: tmp file in same dir + os.replace.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _truncate_content(text: str) -> tuple[str, bool]:
    if len(text) <= HEAD_CAP + TAIL_CAP:
        return text, False
    omitted = len(text) - HEAD_CAP - TAIL_CAP
    return (
        text[:HEAD_CAP]
        + f"\n\n[... {omitted} chars truncated (head {HEAD_CAP} + tail {TAIL_CAP} kept) ...]\n\n"
        + text[-TAIL_CAP:],
        True,
    )


# ---------- section: candidates ----------


def candidates(threshold: float = DEFAULT_THRESHOLD,
               min_n: int = DEFAULT_MIN_N) -> list[dict]:
    """Skills whose Bayesian-shrunk decayed score < threshold with n >= min_n,
    restricted to names that resolve to a local SKILL.md. Worst first."""
    if min_n < 1:
        raise ValueError("min_n must be >= 1")
    db.init_db()
    conn = db._connect()
    try:
        rows = conn.execute(
            """
            SELECT tool_name, rating,
                   julianday('now') - julianday(rated_at, 'utc') AS age_days
            FROM ratings
            """
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for name, rating, age in rows:
        grouped[name].append((int(rating), float(age) if age is not None else 0.0))

    out: list[dict] = []
    for name, entries in grouped.items():
        stats = db._compute_stats(entries)
        if stats["n"] < min_n:
            continue
        score = db._shrunk_score(stats)
        if score >= threshold:
            continue
        path = _skill_md_path(name)
        if path is None:
            continue  # rated, but not a rewritable local skill (mcp/plugin/missing)
        out.append({
            "skill": name,
            "score": round(score, 3),
            "n": stats["n"],
            "decayed_avg": round(stats["decayed_avg"], 3),
            "path": str(path),
        })
    out.sort(key=lambda d: (d["score"], -d["n"]))
    return out


# ---------- section: package ----------


def package(skill_name: str) -> dict:
    """Bundle everything Claude needs to rewrite a skill: current content,
    rating history, and the prior-attempt ledger (so discarded proposals are
    never re-proposed)."""
    path = _skill_md_path(skill_name)
    if path is None:
        raise FileNotFoundError(f"no local SKILL.md found for skill {skill_name!r}")
    text = path.read_text(encoding="utf-8", errors="replace")
    content, truncated = _truncate_content(text)

    stats = db.get_rating_stats(skill_name)
    db.init_db()
    conn = db._connect()
    try:
        rating_rows = conn.execute(
            """
            SELECT rating, rated_at FROM ratings
            WHERE tool_name = ? ORDER BY id DESC LIMIT ?
            """,
            (db._normalize(skill_name), RATING_HISTORY_LIMIT),
        ).fetchall()
    finally:
        conn.close()

    versions = db.get_skill_versions(skill_name, 10)
    prior_attempts = [
        {"id": v["id"], "generation": v["generation"], "proposal": v["proposal"],
         "outcome": v["outcome"], "baseline_score": v["baseline_score"],
         "created_at": v["created_at"]}
        for v in versions
    ]

    # Frontier context so the rewriting model knows the exact bar to clear: the
    # champion score and how many prior generations were already discarded
    # against it. None on a never-improved skill (no frontier seeded yet).
    frontier = db.get_frontier(skill_name)
    discarded_count = sum(1 for v in versions if v["outcome"] == "discarded")

    return {
        "skill": db._normalize(skill_name),
        "path": str(path),
        "content": content,
        "content_truncated": truncated,
        "content_chars": len(text),
        "shrunk_score": round(db._shrunk_score(stats), 3),
        "rating_stats": stats,
        "rating_history": [{"rating": r, "rated_at": t} for r, t in rating_rows],
        "prior_attempts": prior_attempts,
        "frontier_score": round(float(frontier["score"]), 3) if frontier else None,
        "frontier_generation": frontier["generation"] if frontier else None,
        "discarded_generations": discarded_count,
    }


# ---------- section: commit ----------


def _set_version_lineage(version_id: int, parent_generation: Optional[int]) -> None:
    """Backfill parent_generation on a freshly-saved version row.

    save_skill_version() (owned by toolforge_db) writes generation but not the
    v8 lineage columns, so the parent pointer is stamped here through the same
    canonical connection helper the rest of this module already queries with.
    """
    conn = db._connect()
    try:
        conn.execute(
            "UPDATE skill_versions SET parent_generation = ? WHERE id = ?",
            (parent_generation, int(version_id)),
        )
        conn.commit()
    finally:
        conn.close()


def commit(skill_name: str, new_md_path: str,
           proposal: Optional[str] = None) -> dict:
    """Snapshot the full original into skill_versions, then atomically replace
    SKILL.md with the rewrite.

    Frontier seeding: the first-ever commit for a skill seeds skill_frontier at
    generation 0 (the original, untouched content) with the frozen baseline
    score, so generation 0 is the bar every later rewrite must clear. The new
    version records parent_generation = the frontier generation it was forked
    from (NULL only if seeding somehow failed). Baseline = current shrunk score,
    frozen now so it is comparable across the pass."""
    path = _skill_md_path(skill_name)
    if path is None:
        raise FileNotFoundError(f"no local SKILL.md found for skill {skill_name!r}")

    pending = [v for v in db.get_skill_versions(skill_name, 10)
               if v["outcome"] == "pending"]
    if pending:
        raise ValueError(
            f"skill {skill_name!r} already has a pending rewrite (version "
            f"{pending[0]['id']}); run verdict or rollback before committing again"
        )

    new_p = Path(new_md_path)
    new_text = new_p.read_text(encoding="utf-8")
    if not new_text.strip():
        raise ValueError("new SKILL.md is empty")
    if len(new_text.encode("utf-8")) > NEW_MD_CAP:
        raise ValueError(f"new SKILL.md exceeds {NEW_MD_CAP} byte cap")

    old_text = path.read_text(encoding="utf-8", errors="replace")
    baseline = db._shrunk_score(db.get_rating_stats(skill_name))

    # Seed the frontier on the first-ever commit: generation 0 is the original
    # content, scored at the frozen baseline. old_text (the live content right
    # now) is the generation-0 content, and it is also captured as this version's
    # backup, so a later DISCARD restores it via the rollback machinery.
    frontier = db.get_frontier(skill_name)
    if frontier is None:
        db.set_frontier(skill_name, 0, baseline)
        frontier = db.get_frontier(skill_name)
    parent_generation = frontier["generation"] if frontier else None

    version_id = db.save_skill_version(skill_name, str(path), old_text,
                                       proposal, baseline)
    _set_version_lineage(version_id, parent_generation)
    _atomic_write_text(path, new_text)
    return {
        "version_id": version_id,
        "skill": db._normalize(skill_name),
        "path": str(path),
        "baseline_score": round(baseline, 3),
        "frontier_score": round(float(frontier["score"]), 3) if frontier else None,
        "parent_generation": parent_generation,
    }


# ---------- section: verdict ----------


def _set_version_eval_score(version_id: int, eval_score: float) -> None:
    """Record the post-rewrite shrunk score on a version row (v8 eval_score)."""
    conn = db._connect()
    try:
        conn.execute(
            "UPDATE skill_versions SET eval_score = ? WHERE id = ?",
            (float(eval_score), int(version_id)),
        )
        conn.commit()
    finally:
        conn.close()


def verdict(skill_name: str) -> dict:
    """Frontier update-or-discard gate over ratings newer than the pending version.

    The bar is the frontier score (the best generation kept so far), NOT merely
    the commit-time baseline — a rewrite must beat the current champion, not just
    the pre-rewrite reputation.

    PROMOTE  — new score beats the frontier; outcome 'improved', frontier advances
               to this generation, eval_score recorded.
    DISCARD  — new score does not beat the frontier; SKILL.md is auto-reverted to
               the frontier content (via the rollback machinery), outcome
               'discarded', frontier unchanged.
    PENDING  — fewer than MIN_VERDICT_RATINGS new ratings so far.
    """
    versions = db.get_skill_versions(skill_name, 10)
    pending = next((v for v in versions if v["outcome"] == "pending"), None)
    if pending is None:
        raise LookupError(f"no pending rewrite for skill {skill_name!r}")

    db.init_db()
    conn = db._connect()
    try:
        # ISO-8601 UTC timestamps compare correctly as strings.
        rows = conn.execute(
            """
            SELECT rating, julianday('now') - julianday(rated_at, 'utc') AS age_days
            FROM ratings WHERE tool_name = ? AND rated_at > ?
            """,
            (db._normalize(skill_name), pending["created_at"]),
        ).fetchall()
    finally:
        conn.close()
    entries = [(int(r), float(a) if a is not None else 0.0) for r, a in rows]

    baseline = pending["baseline_score"]
    if baseline is None:
        # Commit always freezes a baseline; a NULL here means the stored
        # snapshot was tampered with or partially deleted. Never judge
        # against a default prior.
        raise LookupError(
            f"data-integrity error: pending version {pending['id']} for skill "
            f"{skill_name!r} has no baseline_score; refusing verdict "
            "(restore ratings/version row or rollback manually)"
        )

    frontier = db.get_frontier(skill_name)
    if frontier is None:
        # commit() always seeds the frontier; a missing row means the frontier
        # was deleted out from under an in-flight rewrite. Never judge against a
        # default prior.
        raise LookupError(
            f"data-integrity error: skill {skill_name!r} has a pending version "
            f"{pending['id']} but no frontier row; refusing verdict "
            "(rollback manually or restore skill_frontier)"
        )
    frontier_score = float(frontier["score"])

    result = {
        "skill": db._normalize(skill_name),
        "version_id": pending["id"],
        "generation": pending["generation"],
        "parent_generation": frontier["generation"],
        "baseline_score": round(float(baseline), 3),
        "frontier_score": round(frontier_score, 3),
        "new_ratings": len(entries),
        "needed": MIN_VERDICT_RATINGS,
    }
    if len(entries) < MIN_VERDICT_RATINGS:
        result["verdict"] = "PENDING"
        return result

    new_score = db._shrunk_score(db._compute_stats(entries))
    result["new_score"] = round(new_score, 3)
    _set_version_eval_score(pending["id"], new_score)
    if new_score > frontier_score:
        db.set_skill_version_outcome(pending["id"], "improved")
        db.set_frontier(skill_name, pending["generation"], new_score)
        result["verdict"] = "PROMOTE"
    else:
        # DISCARD: restore the frontier content (the pending version's backup IS
        # the frontier content — every commit backs up the live = frontier text,
        # and live only changes on a PROMOTE) and mark the proposal discarded so
        # the ledger never re-proposes it. Frontier is left untouched.
        path = Path(pending["skill_md_path"])
        _atomic_write_text(path, pending["skill_md_backup"])
        db.set_skill_version_outcome(pending["id"], "discarded")
        result["verdict"] = "DISCARD"
        result["reverted_to_generation"] = frontier["generation"]
        result["restored_chars"] = len(pending["skill_md_backup"])
    return result


# ---------- section: rollback ----------


def rollback(skill_name: str) -> dict:
    """Atomic-restore the latest pending backup; outcome -> rolled_back."""
    versions = db.get_skill_versions(skill_name, 10)
    pending = next((v for v in versions if v["outcome"] == "pending"), None)
    if pending is None:
        raise LookupError(f"no pending rewrite to roll back for skill {skill_name!r}")
    path = Path(pending["skill_md_path"])
    _atomic_write_text(path, pending["skill_md_backup"])
    db.set_skill_version_outcome(pending["id"], "rolled_back")
    return {
        "version_id": pending["id"],
        "skill": db._normalize(skill_name),
        "path": str(path),
        "restored_chars": len(pending["skill_md_backup"]),
    }


# ---------- section: lineage ----------


def lineage(skill_name: str) -> dict:
    """Full version chain for a skill plus the current frontier marker.

    Reads parent_generation / eval_score / outcome / baseline_score directly:
    db.get_skill_versions does not project the v8 lineage columns, so the chain
    is read through the canonical connection helper (oldest generation first)."""
    name = db._normalize(skill_name)
    db.init_db()
    conn = db._connect()
    try:
        rows = conn.execute(
            """SELECT id, generation, parent_generation, outcome,
                      baseline_score, eval_score, created_at
               FROM skill_versions
               WHERE skill_name = ?
               ORDER BY generation ASC, id ASC""",
            (name,),
        ).fetchall()
    finally:
        conn.close()
    versions = [
        {"id": r[0], "generation": r[1], "parent_generation": r[2],
         "outcome": r[3], "baseline_score": r[4], "eval_score": r[5],
         "created_at": r[6]}
        for r in rows
    ]
    frontier = db.get_frontier(skill_name)
    return {
        "skill": name,
        "frontier": frontier,
        "versions": versions,
    }


def _fmt_score(value: Optional[float]) -> str:
    return f"{value:.3f}" if value is not None else "  -  "


def render_lineage(data: dict) -> str:
    """ASCII tree of the version chain, matching the repo's status() box style."""
    skill = data["skill"]
    frontier = data["frontier"]
    versions = data["versions"]
    frontier_gen = frontier["generation"] if frontier else None

    lines = [
        "================ ToolForge Lineage ================",
        f"Skill: {skill}",
    ]
    if frontier:
        lines.append(
            f"Frontier: generation {frontier['generation']} "
            f"(score {_fmt_score(frontier['score'])})"
        )
    else:
        lines.append("Frontier: (none - no commits yet)")
    lines.append("")
    if not versions:
        lines.append("  (no versions recorded)")
        lines.append("==================================================")
        return "\n".join(lines)

    lines.append("Version chain (oldest first):")
    last = len(versions) - 1
    # ASCII connectors only: the repo's status() box stays pure ASCII so output
    # survives a cp1252 Windows console (box-drawing glyphs raise there).
    for i, v in enumerate(versions):
        connector = "\\-" if i == last else "+-"
        marker = " *FRONTIER*" if v["generation"] == frontier_gen else ""
        parent = v["parent_generation"]
        parent_disp = f"gen {parent}" if parent is not None else "root"
        lines.append(
            f"  {connector} gen {v['generation']} (parent {parent_disp})  "
            f"{v['outcome']:<11}  baseline {_fmt_score(v['baseline_score'])}  "
            f"eval {_fmt_score(v['eval_score'])}  {v['created_at']}{marker}"
        )
    lines.append("==================================================")
    return "\n".join(lines)


# ---------- section: self-test ----------


def _self_test() -> int:
    """Roundtrip against a temp DB + temp skill dir: candidates -> package ->
    commit (seeds frontier) -> verdict PENDING -> rollback -> re-commit ->
    verdict PROMOTE (frontier advances) -> commit -> verdict DISCARD (auto-revert
    to frontier, frontier unchanged) -> commit -> verdict PROMOTE building on the
    frontier -> lineage chain -> package frontier context.

    Does NOT touch ~/.claude/toolforge.db or any real skill directory.
    """
    global _ROOTS_OVERRIDE
    saved_db_path = db.DB_PATH
    tmpdir = Path(tempfile.mkdtemp(prefix="toolforge_improve_test_"))
    db.DB_PATH = tmpdir / "toolforge.db"
    skills_root = tmpdir / "skills"
    _ROOTS_OVERRIDE = [skills_root]
    passed = 0
    failed = 0
    original = "# test-skill\n\nOriginal body that keeps failing users.\n"
    rewrite = "# test-skill\n\nRewritten body with sharper triggers.\n"
    try:
        skill_dir = skills_root / "test-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(original, encoding="utf-8")

        # 1. truncation ladder
        big = "h" * (HEAD_CAP + 100) + "t" * TAIL_CAP
        cut, was_cut = _truncate_content(big)
        small, small_cut = _truncate_content("tiny")
        if (was_cut and cut.startswith("h" * 100) and cut.endswith("t" * 100)
                and not small_cut and small == "tiny"):
            print("OK: head/tail truncation")
            passed += 1
        else:
            print("FAIL: truncation ladder broken")
            failed += 1

        # 2. candidates surfaces low-rated resolvable skill
        for r in (1, 2, 2):
            db.log_rating("test-skill", r)
        for r in (1, 1, 1):  # low score + enough ratings, but no SKILL.md on disk
            db.log_rating("unresolvable-tool", r)
        cands = candidates(threshold=2.8, min_n=3)
        if (len(cands) == 1 and cands[0]["skill"] == "test-skill"
                and cands[0]["n"] == 3 and cands[0]["score"] < 2.8):
            print(f"OK: candidates found test-skill (score {cands[0]['score']})")
            passed += 1
        else:
            print(f"FAIL: candidates got {cands}")
            failed += 1

        # 3. package bundle (no frontier yet — never committed)
        bundle = package("test-skill")
        if (bundle["content"] == original and bundle["prior_attempts"] == []
                and len(bundle["rating_history"]) == 3
                and not bundle["content_truncated"]
                and bundle["frontier_score"] is None
                and bundle["frontier_generation"] is None
                and bundle["discarded_generations"] == 0):
            print("OK: package bundle")
            passed += 1
        else:
            print(f"FAIL: package got {bundle}")
            failed += 1

        # 4. commit snapshots, swaps content, and SEEDS the frontier at gen 0
        draft = tmpdir / "draft.md"
        draft.write_text(rewrite, encoding="utf-8")
        info = commit("test-skill", str(draft), "rewrite triggers")
        versions = db.get_skill_versions("test-skill")
        lin = lineage("test-skill")
        seed = db.get_frontier("test-skill")
        if (skill_md.read_text(encoding="utf-8") == rewrite
                and len(versions) == 1 and versions[0]["outcome"] == "pending"
                and versions[0]["skill_md_backup"] == original
                and versions[0]["generation"] == 1
                and abs(info["baseline_score"] - versions[0]["baseline_score"]) < 1e-3
                # frontier seeded at generation 0 (the original), parent points to it
                and seed is not None and seed["generation"] == 0
                and abs(seed["score"] - info["baseline_score"]) < 1e-3
                and info["parent_generation"] == 0
                and lin["versions"][0]["parent_generation"] == 0):
            print(f"OK: commit + frontier seed (version {info['version_id']}, "
                  f"baseline {info['baseline_score']}, frontier gen 0)")
            passed += 1
        else:
            print(f"FAIL: commit got info={info}, versions={versions}, seed={seed}")
            failed += 1

        # 5. double-commit blocked while pending
        try:
            commit("test-skill", str(draft), "second rewrite")
            print("FAIL: double commit accepted while pending")
            failed += 1
        except ValueError:
            print("OK: double commit blocked while pending")
            passed += 1

        # 6. verdict PENDING with no new ratings
        v = verdict("test-skill")
        if v["verdict"] == "PENDING" and v["new_ratings"] == 0:
            print("OK: verdict PENDING")
            passed += 1
        else:
            print(f"FAIL: verdict got {v}")
            failed += 1

        # 7. rollback restores original atomically
        rb = rollback("test-skill")
        versions = db.get_skill_versions("test-skill")
        if (skill_md.read_text(encoding="utf-8") == original
                and versions[0]["outcome"] == "rolled_back"
                and rb["restored_chars"] == len(original)):
            print("OK: rollback restored original")
            passed += 1
        else:
            print(f"FAIL: rollback got {rb}, versions={versions}")
            failed += 1

        # 8. re-commit (gen 2) then PROMOTE: new score beats the frontier, and
        #    the frontier ADVANCES to gen 2 (the gate is the frontier, not the
        #    commit-time baseline).
        info2 = commit("test-skill", str(draft), "rewrite triggers, take two")
        # Millisecond timestamps: ensure new ratings sort strictly after created_at.
        time.sleep(0.02)
        for r in (5, 5, 5):
            db.log_rating("test-skill", r)
        v2 = verdict("test-skill")
        versions = db.get_skill_versions("test-skill")
        front2 = db.get_frontier("test-skill")
        if (v2["verdict"] == "PROMOTE" and v2["new_score"] > v2["frontier_score"]
                and versions[0]["generation"] == 2
                and versions[0]["outcome"] == "improved"
                and front2 is not None and front2["generation"] == 2
                and abs(front2["score"] - v2["new_score"]) < 1e-3
                # gen 2 was forked from the seeded frontier (gen 0)
                and v2["parent_generation"] == 0):
            print(f"OK: verdict PROMOTE + frontier advance to gen 2 "
                  f"(new {v2['new_score']} > frontier {v2['frontier_score']})")
            passed += 1
        else:
            print(f"FAIL: promote got v2={v2}, versions={versions}, front2={front2}")
            failed += 1

        # 9. DISCARD: a weak gen-3 rewrite that cannot clear the frontier is
        #    auto-reverted to the frontier content, marked discarded, and leaves
        #    the frontier untouched at gen 2.
        weak = "# test-skill\n\nA worse body that regresses the skill.\n"
        weak_draft = tmpdir / "weak.md"
        weak_draft.write_text(weak, encoding="utf-8")
        info3 = commit("test-skill", str(weak_draft), "risky scope cut")
        # File now holds the weak rewrite; backup is the promoted (frontier) text.
        live_after_commit = skill_md.read_text(encoding="utf-8")
        time.sleep(0.02)
        for r in (1, 1, 1):  # post-rewrite ratings well below the frontier score
            db.log_rating("test-skill", r)
        v3 = verdict("test-skill")
        versions = db.get_skill_versions("test-skill")
        front3 = db.get_frontier("test-skill")
        if (v3["verdict"] == "DISCARD"
                and v3["new_score"] <= v3["frontier_score"]
                and live_after_commit == weak
                # reverted back to the frontier content (the promoted rewrite)
                and skill_md.read_text(encoding="utf-8") == rewrite
                and v3["reverted_to_generation"] == 2
                and versions[0]["generation"] == 3
                and versions[0]["outcome"] == "discarded"
                # frontier unchanged: still gen 2 at the promoted score
                and front3 is not None and front3["generation"] == 2
                and abs(front3["score"] - front2["score"]) < 1e-9):
            print(f"OK: verdict DISCARD + auto-revert to frontier gen 2 "
                  f"(new {v3['new_score']} <= frontier {v3['frontier_score']})")
            passed += 1
        else:
            print(f"FAIL: discard got v3={v3}, versions={versions}, "
                  f"front3={front3}, info3={info3}, "
                  f"live={skill_md.read_text(encoding='utf-8')!r}")
            failed += 1

        # 10. second improvement BUILDS ON the promoted frontier: gen 4 forks
        #     from gen 2 (not gen 0, not the discarded gen 3) and, beating the
        #     frontier again, advances it.
        better = "# test-skill\n\nEven sharper triggers and crisp examples.\n"
        better_draft = tmpdir / "better.md"
        better_draft.write_text(better, encoding="utf-8")
        info4 = commit("test-skill", str(better_draft), "add worked examples")
        time.sleep(0.02)
        for r in (5, 5, 5, 5):  # push the decayed score above the gen-2 frontier
            db.log_rating("test-skill", r)
        v4 = verdict("test-skill")
        versions = db.get_skill_versions("test-skill")
        front4 = db.get_frontier("test-skill")
        if (v4["verdict"] == "PROMOTE"
                and info4["parent_generation"] == 2  # forked from promoted frontier
                and v4["parent_generation"] == 2
                and versions[0]["generation"] == 4
                and versions[0]["outcome"] == "improved"
                and front4 is not None and front4["generation"] == 4
                and skill_md.read_text(encoding="utf-8") == better):
            print(f"OK: second improvement builds on promoted frontier "
                  f"(gen 4 parent {v4['parent_generation']}, frontier -> gen 4)")
            passed += 1
        else:
            print(f"FAIL: build-on-frontier got v4={v4}, info4={info4}, "
                  f"versions={versions}, front4={front4}")
            failed += 1

        # 11. lineage exposes the full chain + frontier marker. The promoted
        #     spine (gen 0 seed -> gen 2 -> gen 4) is a chain of length 3.
        lin = lineage("test-skill")
        by_gen = {v["generation"]: v for v in lin["versions"]}
        promoted_spine = [g for g in (0, 2, 4)
                          if g == 0 or by_gen.get(g, {}).get("outcome") == "improved"]
        rendered = render_lineage(lin)
        if (lin["frontier"] is not None and lin["frontier"]["generation"] == 4
                and len(lin["versions"]) == 4
                # promoted-frontier spine length is exactly 3 (gen 0 -> 2 -> 4)
                and len(promoted_spine) == 3 and promoted_spine == [0, 2, 4]
                # parent pointers chain through the frontier, never the discard
                and by_gen[2]["parent_generation"] == 0
                and by_gen[4]["parent_generation"] == 2
                and by_gen[3]["outcome"] == "discarded"
                and "*FRONTIER*" in rendered):
            print(f"OK: lineage chain (promoted spine {promoted_spine}, "
                  f"4 versions, frontier gen 4)")
            passed += 1
        else:
            print(f"FAIL: lineage got {lin}")
            failed += 1

        # 12. package now carries frontier context for the rewriting model.
        bundle2 = package("test-skill")
        if (bundle2["frontier_generation"] == 4
                and bundle2["frontier_score"] is not None
                and bundle2["discarded_generations"] == 1):
            print(f"OK: package frontier context (frontier gen "
                  f"{bundle2['frontier_generation']}, {bundle2['discarded_generations']} discarded)")
            passed += 1
        else:
            print(f"FAIL: package frontier context got {bundle2}")
            failed += 1

    finally:
        db.DB_PATH = saved_db_path
        _ROOTS_OVERRIDE = None
        # Force re-init on the real path next time: the guard is path-keyed, no leak.
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- section: cli ----------


def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_improve.py candidates [--threshold 2.8] [--min-n 3] [--json]\n"
        "  toolforge_improve.py package <skill> [--json]\n"
        "  toolforge_improve.py commit <skill> <new_md_path> [--proposal <text>]\n"
        "  toolforge_improve.py verdict <skill>\n"
        "  toolforge_improve.py rollback <skill>\n"
        "  toolforge_improve.py lineage <skill> [--json]\n"
        "  toolforge_improve.py --self-test\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    cmd = argv[1]
    try:
        if cmd == "candidates":
            threshold = DEFAULT_THRESHOLD
            min_n = DEFAULT_MIN_N
            as_json = False
            args = argv[2:]
            i = 0
            while i < len(args):
                if args[i] == "--threshold":
                    threshold = float(args[i + 1])
                    i += 2
                elif args[i] == "--min-n":
                    min_n = int(args[i + 1])
                    i += 2
                elif args[i] == "--json":
                    as_json = True
                    i += 1
                else:
                    raise ValueError(f"unknown argument {args[i]!r}")
            cands = candidates(threshold, min_n)
            if as_json:
                print(json.dumps(cands))
            elif not cands:
                print(f"No improvable skills below score {threshold} with >= {min_n} ratings.")
            else:
                print(f"Improve candidates (shrunk score < {threshold}, n >= {min_n}, worst first):")
                for c in cands:
                    print(f"  {c['skill']:<30} score {c['score']:.2f}  "
                          f"({c['n']} rating(s))  {c['path']}")
            return 0 if cands else 1
        if cmd == "package":
            as_json = "--json" in argv
            args = [a for a in argv[2:] if a != "--json"]
            bundle = package(args[0])
            print(json.dumps(bundle) if as_json else json.dumps(bundle, indent=2))
            return 0
        if cmd == "commit":
            proposal = None
            args = argv[2:]
            if "--proposal" in args:
                idx = args.index("--proposal")
                proposal = args[idx + 1]
                args = args[:idx] + args[idx + 2:]
            info = commit(args[0], args[1], proposal)
            print(json.dumps(info))
            return 0
        if cmd == "verdict":
            try:
                result = verdict(argv[2])
            except LookupError as exc:
                print(f"toolforge_improve: {exc}", file=sys.stderr)
                return 1
            print(result["verdict"])
            print(json.dumps(result))
            return 0
        if cmd == "rollback":
            try:
                result = rollback(argv[2])
            except LookupError as exc:
                print(f"toolforge_improve: {exc}", file=sys.stderr)
                return 1
            print(json.dumps(result))
            return 0
        if cmd == "lineage":
            as_json = "--json" in argv
            args = [a for a in argv[2:] if a != "--json"]
            data = lineage(args[0])
            if as_json:
                print(json.dumps(data))
            else:
                print(render_lineage(data))
            return 0 if data["versions"] else 1
        if cmd == "--self-test":
            return _self_test()
    except sqlite3.Error as exc:
        print(f"toolforge_improve sqlite error: {exc}", file=sys.stderr)
        return 3
    except (OSError, FileNotFoundError) as exc:
        print(f"toolforge_improve os error: {exc}", file=sys.stderr)
        return 3
    except (IndexError, ValueError) as exc:
        print(f"toolforge_improve error: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2

    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
