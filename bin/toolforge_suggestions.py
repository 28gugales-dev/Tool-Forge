#!/usr/bin/env python3
"""ToolForge suggestion engine. Matches a task description or task-type ID to
curated skill/MCP recommendations from catalog/suggestions.json.

CLI output is JSON. Exit 0 on success, 1 if no suggestions found, 2 on usage
error, 3 on file I/O error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

_CATALOG_DIR = Path(__file__).parent.parent / "catalog"
_SUGGESTIONS_FILE = _CATALOG_DIR / "suggestions.json"

_STOP_WORDS = {
    "a", "an", "the", "and", "or", "to", "for", "in", "with", "of",
    "how", "do", "i", "we", "can", "need", "want", "help", "please",
    "build", "create", "write", "make", "add", "get", "use",
}


def _load_suggestions() -> dict:
    try:
        with _SUGGESTIONS_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"toolforge_suggestions: cannot load suggestions.json: {exc}", file=sys.stderr)
        sys.exit(3)


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-zA-Z0-9/.#@+-]+", text.lower())
    return {t for t in tokens if t not in _STOP_WORDS and len(t) > 1}


def _score_task_type(task_tokens: set[str], task_type: dict) -> float:
    kw_tokens = _tokenize(" ".join(task_type.get("keywords", [])))
    label_tokens = _tokenize(task_type.get("label", ""))
    id_tokens = _tokenize(task_type.get("id", "").replace("-", " "))
    all_kw = kw_tokens | label_tokens | id_tokens
    if not all_kw:
        return 0.0
    overlap = task_tokens & all_kw
    # Jaccard-style: intersection / union, weighted by label match
    base = len(overlap) / len(all_kw | task_tokens) if (all_kw | task_tokens) else 0.0
    # Boost for label word matches (more signal than keyword bag)
    label_overlap = task_tokens & label_tokens
    boost = 0.15 * len(label_overlap) if label_overlap else 0.0
    return min(1.0, base + boost)


def match_task(query: str, top_n: int = 3, threshold: float = 0.05) -> list[dict]:
    """Returns up to top_n matching task types with their suggestions, scored by relevance."""
    data = _load_suggestions()
    task_types = data.get("task_types", [])
    if not task_types:
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    scored = []
    for tt in task_types:
        score = _score_task_type(query_tokens, tt)
        if score >= threshold:
            scored.append((score, tt))

    scored.sort(key=lambda x: -x[0])
    results = []
    for score, tt in scored[:top_n]:
        results.append({
            "task_type_id": tt["id"],
            "label": tt["label"],
            "score": round(score, 3),
            "suggestions": tt.get("suggestions", []),
        })
    return results


def get_by_id(task_type_id: str) -> Optional[dict]:
    """Exact lookup by task_type id."""
    data = _load_suggestions()
    for tt in data.get("task_types", []):
        if tt["id"] == task_type_id:
            return {"task_type_id": tt["id"], "label": tt["label"], "suggestions": tt["suggestions"]}
    return None


def list_all() -> list[dict]:
    data = _load_suggestions()
    return [
        {"id": tt["id"], "label": tt["label"], "suggestion_count": len(tt.get("suggestions", []))}
        for tt in data.get("task_types", [])
    ]


def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_suggestions.py match <query text>\n"
        "  toolforge_suggestions.py get <task_type_id>\n"
        "  toolforge_suggestions.py list\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_usage(), file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "match":
        if len(argv) < 3:
            print("match requires a query", file=sys.stderr)
            return 2
        query = " ".join(argv[2:])
        results = match_task(query)
        if not results:
            print(json.dumps([]))
            return 1
        print(json.dumps(results, indent=2))
        return 0
    if cmd == "get":
        if len(argv) < 3:
            print("get requires a task_type_id", file=sys.stderr)
            return 2
        result = get_by_id(argv[2])
        print(json.dumps(result))
        return 0 if result else 1
    if cmd == "list":
        print(json.dumps(list_all(), indent=2))
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
