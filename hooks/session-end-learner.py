#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""SessionStop hook — adaptive learning on session end.

Fires when a Claude Code session ends. Reads the session's tool usage from
the ToolForge DB, infers the dominant task type from the pipeline history,
records preference signals so the user profile adapts, detects new workflow
shortcuts, and optionally pushes a summary to Hermes/Obsidian.

Config keys in ~/.claude/toolforge-config.json:
  "learner_push_hermes": true/false    (default: false)
  "learner_push_obsidian": true/false  (default: false)
  "learner_task_type": "<override>"    (if set, always uses this task type)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PLUGIN_ROOT / "bin"
WEBUI_DIR = PLUGIN_ROOT / "webui"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
CONFIG_PATH = CLAUDE_HOME / "toolforge-config.json"
LOG_PATH = CLAUDE_HOME / "toolforge_learner.log"

MAX_STDIN = 32 * 1024


def _read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _log(entry: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _infer_task_type(skills_used: list[str]) -> str:
    """Heuristic: map skill names to known task type IDs using keyword overlap."""
    skill_str = " ".join(skills_used).lower()
    mapping = [
        ("frontend-ui",      ["ui", "frontend", "component", "shadcn", "magic", "react", "svelte"]),
        ("backend-api",      ["api", "backend", "fastapi", "flask", "express", "rest", "grpc"]),
        ("data-analysis",    ["pandas", "data", "csv", "excel", "analysis", "notebook", "jupyter"]),
        ("database",         ["sql", "postgres", "mysql", "sqlite", "db", "database", "prisma"]),
        ("devops-infra",     ["docker", "k8s", "terraform", "ci", "deploy", "aws", "gcp", "azure"]),
        ("testing",          ["test", "pytest", "jest", "playwright", "cypress", "coverage"]),
        ("documentation",    ["docs", "readme", "markdown", "docstring", "wiki"]),
        ("code-review",      ["review", "lint", "audit", "sonar", "security"]),
        ("ai-agents",        ["agent", "llm", "openai", "anthropic", "langchain", "crew"]),
        ("web-search",       ["search", "browser", "web", "crawl", "scrape"]),
    ]
    for task_id, keywords in mapping:
        if any(kw in skill_str for kw in keywords):
            return task_id
    return "general"


def main() -> int:
    raw = sys.stdin.read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        return 0

    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0

    session_id = event.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "unknown")
    cfg = _read_config()

    skills_used: list[str] = []
    task_type = cfg.get("learner_task_type", "")
    error = ""
    shortcut_count = 0

    try:
        import toolforge_db as db
        import toolforge_user_profile as profile

        # Collect skills from recent usage_stats written in this session
        db.init_db()
        conn = db._connect()
        try:
            rows = conn.execute(
                "SELECT tool_key FROM usage_stats WHERE last_used_at >= datetime('now', '-2 hours') ORDER BY count_30d DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()

        skills_used = [r[0].split(":", 1)[-1] for r in rows]

        if not task_type:
            task_type = _infer_task_type(skills_used)

        if skills_used and task_type:
            profile.record_session_signals(task_type, skills_used)

        # Shortcut detection reads the pipelines table, which may be absent on a
        # partially-migrated DB. Isolate it so a failure here doesn't discard the
        # preference signals already recorded above.
        try:
            newly_detected = profile.detect_shortcuts_from_pipelines()
            shortcut_count = len(newly_detected)
        except Exception as exc:
            error += f" shortcuts:{exc}"

        # Optionally push to Hermes
        if cfg.get("learner_push_hermes") and skills_used:
            try:
                from integrations import hermes as hermes_mod
                hermes_mod.push_session_summary(session_id, task_type, skills_used)
            except Exception as exc:
                error += f" hermes:{exc}"

        # Optionally push to Obsidian
        if cfg.get("learner_push_obsidian") and skills_used:
            try:
                from integrations import obsidian as obs_mod
                obs_mod.write_session_note(session_id, task_type, skills_used)
            except Exception as exc:
                error += f" obsidian:{exc}"

    except Exception as exc:
        error = str(exc)
        shortcut_count = 0

    _log({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "task_type": task_type,
        "skills_recorded": skills_used,
        "shortcuts_detected": shortcut_count,
        "error": error or None,
    })

    return 0


if __name__ == "__main__":
    sys.exit(main())
