#!/usr/bin/env python3
"""ToolForge pipeline engine. Powers the /forge meta-skill.

Responsibilities:
  - suggest_skill(sub_task)  Route a sub-task description to installed skills
  - compute_steps_hash(steps) Stable hash of a skill-chain for similarity lookup
  - save(task_desc, steps, success)  Persist a pipeline to SQLite
  - similar(steps_hash)  Retrieve past pipelines with the same skill chain
  - recent([limit])  Most recently run pipelines
  - render(steps)  Plain-ASCII flowchart for chat display

CLI (every command prints JSON or plain text to stdout):
  toolforge_pipeline.py suggest-skill <sub-task text...>
  toolforge_pipeline.py save <task_desc> <steps_hash> <steps_json> [--success]
  toolforge_pipeline.py similar <steps_hash> [<limit>]
  toolforge_pipeline.py recent [--limit N]
  toolforge_pipeline.py render <steps_json>
  toolforge_pipeline.py hash <steps_json>
  toolforge_pipeline.py --self-test
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


# ── step schema ────────────────────────────────────────────────────────────────
# Each step object:
# {
#   "step":       int,       1-based position in chain
#   "skill_name": str,       installed skill name, or "(built-in)"
#   "skill_type": str,       "skill" | "mcp" | "plugin" | "builtin"
#   "sub_task":   str,       one-sentence description of what this step does
#   "score":      float,     router relevance score (0.0 for built-in)
#   "description":str,       skill description (empty for built-in)
# }

BUILTIN_STEP = {
    "skill_name": "(built-in)",
    "skill_type": "builtin",
    "score": 0.0,
    "description": "",
}


# ── router integration ─────────────────────────────────────────────────────────

def suggest_skill(sub_task: str, top_k: int = 3) -> list[dict]:
    """Return top matching skills for a sub-task, or built-in fallback."""
    if not sub_task.strip():
        return []
    try:
        import toolforge_router  # type: ignore
        results = toolforge_router.route(sub_task)
        if results:
            return [
                {
                    "skill_name": r["name"],
                    "skill_type": "skill",
                    "score": r["score"],
                    "description": r.get("description", ""),
                }
                for r in results[:top_k]
            ]
    except Exception:
        pass
    return [{**BUILTIN_STEP}]


# ── hashing ────────────────────────────────────────────────────────────────────

def compute_steps_hash(steps: list[dict]) -> str:
    """Stable 16-char hex hash of the ordered skill-name chain.

    Two pipelines share a hash only when they use the same skills in the same
    order, regardless of sub_task wording — enabling similarity matching even
    when task descriptions differ.
    """
    chain = ":".join(s.get("skill_name", "(built-in)") for s in steps)
    return hashlib.sha256(chain.encode("utf-8")).hexdigest()[:16]


# ── ASCII renderer ─────────────────────────────────────────────────────────────

def render_ascii(task_desc: str, steps: list[dict]) -> str:
    """Return a plain-ASCII flowchart string suitable for chat display."""
    width = 60
    lines: list[str] = []

    header = f'forge: "{task_desc}"'
    lines += [
        "=" * width,
        header[:width],
        "=" * width,
        "",
    ]

    skill_col = 22
    for i, step in enumerate(steps):
        num = step.get("step", i + 1)
        name = step.get("skill_name", "(built-in)")
        sub = step.get("sub_task", "")
        score = step.get("score", 0.0)
        score_tag = f"  [{score:.2f}]" if score > 0 else ""

        name_field = f"{num}. {name}"
        lines.append(f"  {name_field:<{skill_col}}  {sub[:38]}{score_tag}")
        if i < len(steps) - 1:
            lines.append(f"  {'':>{skill_col + 2}}|")

    lines.append("")

    n_skills = sum(1 for s in steps if s.get("skill_type") != "builtin")
    n_builtin = len(steps) - n_skills
    parts = []
    if n_skills:
        parts.append(f"{n_skills} skill(s)")
    if n_builtin:
        parts.append(f"{n_builtin} built-in step(s)")
    lines.append("  " + "  |  ".join(parts))
    lines.append("")
    lines.append("  Proceed? [Y/n]  Skip a step? (type 'skip N')  Edit? (type step number)")
    return "\n".join(lines)


# ── DB persistence ─────────────────────────────────────────────────────────────

def save_pipeline(task_desc: str, steps: list[dict], success: bool = False) -> int:
    """Persist pipeline to DB. Returns row id."""
    import toolforge_db  # type: ignore
    steps_hash = compute_steps_hash(steps)
    steps_json = json.dumps(steps)
    return toolforge_db.save_pipeline(task_desc, steps_hash, steps_json, success)


def get_similar_pipelines(steps_hash: str, limit: int = 3) -> list[dict]:
    import toolforge_db  # type: ignore
    return toolforge_db.get_pipelines_by_hash(steps_hash, limit)


def get_recent_pipelines(limit: int = 10) -> list[dict]:
    import toolforge_db  # type: ignore
    return toolforge_db.get_recent_pipelines(limit)


# ── self-test ──────────────────────────────────────────────────────────────────

def _self_test() -> int:
    passed = failed = 0

    # compute_steps_hash: stable + order-sensitive
    steps_a = [{"skill_name": "code-review"}, {"skill_name": "playwright-testing"}]
    steps_b = [{"skill_name": "playwright-testing"}, {"skill_name": "code-review"}]
    steps_c = [{"skill_name": "code-review"}, {"skill_name": "playwright-testing"}]
    h_a, h_b, h_c = compute_steps_hash(steps_a), compute_steps_hash(steps_b), compute_steps_hash(steps_c)
    if h_a == h_c and h_a != h_b:
        print("OK: steps_hash is stable and order-sensitive")
        passed += 1
    else:
        print(f"FAIL: hash stability — a={h_a} b={h_b} c={h_c}")
        failed += 1

    # render_ascii: contains skill names
    mock_steps = [
        {"step": 1, "skill_name": "sql-schema", "skill_type": "skill",
         "sub_task": "Design auth schema", "score": 0.72},
        {"step": 2, "skill_name": "(built-in)", "skill_type": "builtin",
         "sub_task": "Implement auth endpoints", "score": 0.0},
        {"step": 3, "skill_name": "playwright-testing", "skill_type": "skill",
         "sub_task": "Write E2E tests", "score": 0.61},
        {"step": 4, "skill_name": "code-review", "skill_type": "skill",
         "sub_task": "Security review", "score": 0.58},
    ]
    rendered = render_ascii("build auth feature with tests", mock_steps)
    if ("sql-schema" in rendered and "playwright-testing" in rendered
            and "code-review" in rendered and "Proceed?" in rendered):
        print("OK: render_ascii contains all skill names and prompt")
        passed += 1
    else:
        print(f"FAIL: render_ascii missing content:\n{rendered}")
        failed += 1

    # render_ascii: score tags present for non-builtin, absent for builtin
    if "[0.72]" in rendered and "[0.61]" in rendered and "[0.00]" not in rendered:
        print("OK: render_ascii shows scores for skills, omits for built-in")
        passed += 1
    else:
        print(f"FAIL: score rendering in:\n{rendered}")
        failed += 1

    # suggest_skill: returns list (may be empty if no skills installed)
    result = suggest_skill("animate React component with GSAP scroll")
    if isinstance(result, list):
        print(f"OK: suggest_skill returns list ({len(result)} item(s) from real install)")
        passed += 1
    else:
        print(f"FAIL: suggest_skill returned {type(result)}")
        failed += 1

    # suggest_skill: empty sub_task returns []
    result_empty = suggest_skill("")
    if result_empty == []:
        print("OK: suggest_skill('') returns []")
        passed += 1
    else:
        print(f"FAIL: suggest_skill('') returned {result_empty}")
        failed += 1

    # DB round-trip (uses temp DB via toolforge_db global swap)
    import toolforge_db  # type: ignore
    import tempfile
    original_db = toolforge_db.DB_PATH
    tmp = Path(tempfile.mkdtemp()) / "pipeline_test.db"
    toolforge_db.DB_PATH = tmp
    try:
        toolforge_db.init_db()
        pid = save_pipeline("build auth feature", mock_steps, success=True)
        h = compute_steps_hash(mock_steps)
        similar = get_similar_pipelines(h)
        recent = get_recent_pipelines(5)
        if (pid > 0
                and similar and similar[0]["success"] is True
                and recent and recent[0]["id"] == pid):
            print("OK: pipeline DB round-trip")
            passed += 1
        else:
            print(f"FAIL: DB round-trip pid={pid} similar={similar} recent={recent}")
            failed += 1
    finally:
        toolforge_db.DB_PATH = original_db
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ── CLI ────────────────────────────────────────────────────────────────────────

def _usage_str() -> str:
    return (
        "Usage:\n"
        "  toolforge_pipeline.py suggest-skill <sub-task text...>\n"
        "  toolforge_pipeline.py save <task_desc> <steps_hash> <steps_json> [--success]\n"
        "  toolforge_pipeline.py similar <steps_hash> [<limit>]\n"
        "  toolforge_pipeline.py recent [--limit N]\n"
        "  toolforge_pipeline.py render <task_desc> <steps_json>\n"
        "  toolforge_pipeline.py hash <steps_json>\n"
        "  toolforge_pipeline.py --self-test\n"
    )


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage_str(), file=sys.stderr)
        return 2

    cmd = argv[0]

    if cmd == "--self-test":
        return _self_test()

    if cmd == "suggest-skill":
        sub_task = " ".join(argv[1:]).strip()
        if not sub_task:
            print("suggest-skill requires a sub-task description", file=sys.stderr)
            return 2
        print(json.dumps(suggest_skill(sub_task)))
        return 0

    if cmd == "hash":
        if len(argv) < 2:
            print("hash requires steps_json", file=sys.stderr)
            return 2
        try:
            steps = json.loads(argv[1])
        except json.JSONDecodeError as exc:
            print(f"invalid steps_json: {exc}", file=sys.stderr)
            return 2
        print(compute_steps_hash(steps))
        return 0

    if cmd == "render":
        if len(argv) < 3:
            print("render requires <task_desc> <steps_json>", file=sys.stderr)
            return 2
        try:
            steps = json.loads(argv[2])
        except json.JSONDecodeError as exc:
            print(f"invalid steps_json: {exc}", file=sys.stderr)
            return 2
        print(render_ascii(argv[1], steps))
        return 0

    if cmd == "save":
        # save <task_desc> <steps_hash> <steps_json> [--success]
        success_flag = "--success" in argv
        args = [a for a in argv[1:] if a != "--success"]
        if len(args) < 3:
            print("save requires <task_desc> <steps_hash> <steps_json>", file=sys.stderr)
            return 2
        try:
            steps = json.loads(args[2])
        except json.JSONDecodeError as exc:
            print(f"invalid steps_json: {exc}", file=sys.stderr)
            return 2
        try:
            import toolforge_db  # type: ignore
            pid = toolforge_db.save_pipeline(args[0], args[1], args[2], success_flag)
            print(pid)
            return 0
        except Exception as exc:
            print(f"save error: {exc}", file=sys.stderr)
            return 3

    if cmd == "similar":
        if len(argv) < 2:
            print("similar requires <steps_hash>", file=sys.stderr)
            return 2
        limit = int(argv[2]) if len(argv) > 2 else 3
        try:
            print(json.dumps(get_similar_pipelines(argv[1], limit)))
            return 0
        except Exception as exc:
            print(f"similar error: {exc}", file=sys.stderr)
            return 3

    if cmd == "recent":
        limit = 10
        if "--limit" in argv:
            idx = argv.index("--limit")
            if idx + 1 < len(argv):
                limit = int(argv[idx + 1])
        try:
            print(json.dumps(get_recent_pipelines(limit)))
            return 0
        except Exception as exc:
            print(f"recent error: {exc}", file=sys.stderr)
            return 3

    print(f"unknown command: {cmd}", file=sys.stderr)
    print(_usage_str(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
