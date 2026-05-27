#!/usr/bin/env python3
"""ToolForge usage detector.

Walks Claude Code session transcripts at ~/.claude/projects/*.jsonl, counts
tool_use invocations per typed tool_key in the last N days, persists to
SQLite usage_stats table for router + audit consumption.

tool_key namespace (typed, matches toolforge_db.TOOL_KEY_RE):
  skill:<name>       — Skill tool with input.skill = <name>
  agent:<name>       — Task/Agent tool with input.subagent_type = <name>
  mcp:<server>       — mcp__<server>__<tool> shape, takes <server>
  builtin:<name>     — anything else (Edit, Bash, Read, Write, Grep, etc.)

Privacy: never logs prompt or user-message content. Reads only tool_use
blocks from assistant messages. Counts + timestamps only. No network.

Caps:
  500 transcripts max
  10 MiB per JSONL (skip oversized files)
  5s wall-clock budget (return partial on overrun)
  Cache TTL 3600s in tempdir
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import toolforge_db  # type: ignore  # noqa: E402

# ---------- section: constants ----------
DEFAULT_DAYS = 30
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")
MAX_TRANSCRIPTS = 500
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB
SCAN_BUDGET_SECONDS = 5.0
CACHE_TTL_SECONDS = 3600

TOOL_KEY_NAMESPACES = ("skill", "agent", "mcp", "builtin")


# ---------- section: tool-key-classification ----------
def _parse_ts(ts: str) -> float:
    """Parse ISO 8601 timestamp to epoch seconds. Returns 0 on failure."""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


# WARN: see SKETCHY_CODE_AUDIT.md#s3-5 — FIXED in F21 (canonical source; imported by toolforge_local_scan.py and toolforge_db.py).
def _normalize_name(s: str) -> str:
    """Lowercase, map colons to slash (plugin:skill namespacing), strip junk.

    Colons must NOT survive — TOOL_KEY_RE uses `:` as the namespace separator
    (skill:foo). A skill named `plugin:skill-foo` becomes `plugin/skill-foo`
    so tool_key = `skill:plugin/skill-foo` stays parse-able.
    """
    s = s.strip().lower()
    out = []
    for ch in s:
        if ch.isalnum() or ch in "._@/-":
            out.append(ch)
        elif ch == ":":
            out.append("/")
        elif ch in " \t":
            out.append("-")
    return "".join(out)[:80]


def _classify(block: dict) -> Optional[str]:
    """Return a typed tool_key from a tool_use block, or None to skip."""
    name = block.get("name") or ""
    if not isinstance(name, str) or not name:
        return None
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Skill":
        skill = _normalize_name(str(inp.get("skill", "")))
        return f"skill:{skill}" if skill else None
    if name in ("Task", "Agent"):
        sub = _normalize_name(str(inp.get("subagent_type", "")))
        return f"agent:{sub}" if sub else None
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 2:
            server = _normalize_name(parts[1])
            return f"mcp:{server}" if server else None
        return None
    return f"builtin:{_normalize_name(name)}"


# ---------- section: scanners ----------
def _iter_transcripts(cutoff_epoch: float) -> list[str]:
    """List candidate JSONL paths with mtime > cutoff. Cap at MAX_TRANSCRIPTS."""
    paths: list[tuple[float, str]] = []
    for p in glob.iglob(PROJECTS_GLOB):
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime < cutoff_epoch:
            continue
        paths.append((st.st_mtime, p))
    paths.sort(reverse=True)
    return [p for _, p in paths[:MAX_TRANSCRIPTS]]


def _scan_one(path: str, cutoff_epoch: float, counts: Counter, last_used: dict) -> tuple[int, int]:
    """Returns (events_scanned, tool_uses_found)."""
    try:
        st = os.stat(path)
        if st.st_size > MAX_FILE_BYTES:
            print(f"usage_detector: skipping {path} ({st.st_size} bytes > {MAX_FILE_BYTES})",
                  file=sys.stderr)
            return 0, 0
    # WARN: see SKETCHY_CODE_AUDIT.md#s4-9 — FIXED in F31 (stderr-logged; stat aborted, caller sees 0,0).
    except OSError as exc:
        print(f"toolforge_usage_detector: stat aborted on {path}: {exc}", file=sys.stderr)
        return 0, 0

    events = 0
    uses = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                events += 1
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") != "assistant":
                    continue
                ts_epoch = _parse_ts(evt.get("timestamp", ""))
                if ts_epoch and ts_epoch < cutoff_epoch:
                    continue
                msg = evt.get("message") if isinstance(evt.get("message"), dict) else {}
                content = msg.get("content") if isinstance(msg.get("content"), list) else []
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    uses += 1
                    key = _classify(block)
                    if key is None:
                        continue
                    counts[key] += 1
                    if ts_epoch > last_used.get(key, 0):
                        last_used[key] = ts_epoch
    # WARN: see SKETCHY_CODE_AUDIT.md#s4-9 — FIXED in F31 (stderr-logged; read aborted mid-file, returns partial).
    except OSError as exc:
        print(f"toolforge_usage_detector: read aborted on {path}: {exc}", file=sys.stderr)
    return events, uses


# ---------- section: public-api ----------
def detect_usage(days: int = DEFAULT_DAYS, force: bool = False) -> dict:
    """Compute usage counts + last-used timestamps over the last N days."""
    if days < 1 or days > 365:
        raise ValueError(f"days must be in 1..365, got {days}")

    cache = _cache_path(days)
    if not force:
        cached = _load_cache(cache)
        if cached is not None:
            return cached

    cutoff = time.time() - days * 86400
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS

    counts: Counter = Counter()
    last_used: dict[str, float] = {}
    paths = _iter_transcripts(cutoff)

    events_scanned = 0
    tool_uses_found = 0
    transcripts_scanned = 0
    partial = False

    for path in paths:
        if time.monotonic() > deadline:
            partial = True
            break
        e, u = _scan_one(path, cutoff, counts, last_used)
        events_scanned += e
        tool_uses_found += u
        transcripts_scanned += 1

    result = {
        "window_days": days,
        "transcripts_scanned": transcripts_scanned,
        "transcripts_available": len(paths),
        "events_scanned": events_scanned,
        "tool_uses_found": tool_uses_found,
        "partial": partial,
        "counts": dict(counts.most_common()),
        "last_used": {k: datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                      for k, v in last_used.items()},
    }
    _save_cache(cache, result)
    return result


# ---------- section: db-persistence ----------
def persist_to_db(result: dict) -> tuple[int, int]:
    """Write usage_stats rows from a detect_usage result.

    Returns (written, failed) where:
      written = rows successfully upserted into usage_stats.
      failed  = rows that raised on persist (ValueError on int coerce, OSError
                on FS-backed DB issue, sqlite3.Error on lock/integrity/etc.).

    The `failed` counter is distinct from any future `skipped` counter (see
    audit §4-9): `skipped` would mean a row was rejected by validation
    BEFORE the DB call; `failed` means the DB call itself raised. Both are
    survivable per-row; the loop continues past either.
    """
    counts = result.get("counts", {})
    last_used = result.get("last_used", {})
    written = 0
    failed = 0
    for key, count in counts.items():
        try:
            toolforge_db.upsert_usage_stats(key, int(count), last_used.get(key))
            written += 1
        # WARN: see SKETCHY_CODE_AUDIT.md#s2-5 — FIXED in F07 (sqlite3.Error now caught; failed counter surfaces drops).
        except (ValueError, OSError, sqlite3.Error) as exc:
            failed += 1
            print(f"usage_detector: skip {key}: {exc}", file=sys.stderr)
    return written, failed


# ---------- section: cache ----------
def _cache_path(days: int) -> Path:
    return Path(tempfile.gettempdir()) / f"toolforge_usage_{days}d.json"


def _load_cache(path: Path) -> Optional[dict]:
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
                f"usage_detector: quarantine of {path} failed: {rn_exc}",
                file=sys.stderr,
            )
        return None
    except OSError as exc:
        # Not FileNotFoundError (caught by .exists() check above) — permission
        # or IO issue worth surfacing.
        print(
            f"usage_detector: cache read failed at {path}: {exc}",
            file=sys.stderr,
        )
        return None


def _save_cache(path: Path, data: dict) -> None:
    try:
        # WARN: see SKETCHY_CODE_AUDIT.md#s4-4 — FIXED in F11 (atomic write via tmp + os.replace).
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"usage_detector: cache write failed: {exc}", file=sys.stderr)


# ---------- section: self-test ----------

def _self_test() -> int:
    passed = 0
    failed = 0

    # 1. classify Skill block
    key = _classify({"type": "tool_use", "name": "Skill", "input": {"skill": "GSAP-React"}})
    if key == "skill:gsap-react":
        print("OK: classify Skill -> skill:<lowered>")
        passed += 1
    else:
        print(f"FAIL: Skill classify got {key}")
        failed += 1

    # 2. classify mcp__server__tool
    key = _classify({"type": "tool_use", "name": "mcp__plugin_github_github__add_issue_comment"})
    if key == "mcp:plugin_github_github":
        print("OK: classify mcp -> mcp:<server>")
        passed += 1
    else:
        print(f"FAIL: mcp classify got {key}")
        failed += 1

    # 3. classify Agent w/ subagent_type
    key = _classify({"type": "tool_use", "name": "Task", "input": {"subagent_type": "Explore"}})
    if key == "agent:explore":
        print("OK: classify Task -> agent:<subagent>")
        passed += 1
    else:
        print(f"FAIL: Task classify got {key}")
        failed += 1

    # 4. classify builtin
    key = _classify({"type": "tool_use", "name": "Edit"})
    if key == "builtin:edit":
        print("OK: classify Edit -> builtin:edit")
        passed += 1
    else:
        print(f"FAIL: Edit classify got {key}")
        failed += 1

    # 5. classify Skill with empty skill name -> None
    key = _classify({"type": "tool_use", "name": "Skill", "input": {}})
    if key is None:
        print("OK: empty Skill returns None")
        passed += 1
    else:
        print(f"FAIL: empty Skill got {key}")
        failed += 1

    # 6. classify garbage block returns None or builtin gracefully
    key = _classify({"type": "tool_use"})
    if key is None:
        print("OK: missing name returns None")
        passed += 1
    else:
        print(f"FAIL: missing name got {key}")
        failed += 1

    # 7. parse_ts handles malformed
    if _parse_ts("not a date") == 0.0 and _parse_ts("") == 0.0:
        print("OK: parse_ts fail-soft")
        passed += 1
    else:
        print("FAIL: parse_ts didn't return 0 on garbage")
        failed += 1

    # 8. detect_usage rejects bad days
    try:
        detect_usage(days=0)
        print("FAIL: days=0 accepted")
        failed += 1
    except ValueError:
        print("OK: days=0 rejected")
        passed += 1

    # 9. synthetic JSONL roundtrip
    tmpdir = Path(tempfile.mkdtemp(prefix="toolforge_usage_test_"))
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fake = tmpdir / "fake_session.jsonl"
        events = [
            {"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "tool_use", "name": "Edit"},
                {"type": "tool_use", "name": "Skill", "input": {"skill": "impeccable"}},
                {"type": "tool_use", "name": "mcp__plugin_context7_context7__query-docs"},
            ]}},
            {"type": "user", "timestamp": ts},  # should be ignored
            {"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "tool_use", "name": "Edit"},
            ]}},
        ]
        fake.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

        counts: Counter = Counter()
        last_used: dict[str, float] = {}
        e, u = _scan_one(str(fake), 0.0, counts, last_used)
        if (counts.get("builtin:edit") == 2
                and counts.get("skill:impeccable") == 1
                and counts.get("mcp:plugin_context7_context7") == 1
                and u == 4):
            print("OK: synthetic JSONL roundtrip")
            passed += 1
        else:
            print(f"FAIL: synthetic got counts={dict(counts)}, uses={u}")
            failed += 1
    finally:
        try:
            for p in tmpdir.iterdir():
                p.unlink()
            tmpdir.rmdir()
        except OSError:
            pass

    # 10. oversized file skipped
    big = Path(tempfile.mkdtemp(prefix="toolforge_big_")) / "big.jsonl"
    try:
        big.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        counts: Counter = Counter()
        last_used: dict[str, float] = {}
        e, u = _scan_one(str(big), 0.0, counts, last_used)
        if e == 0 and u == 0:
            print("OK: oversized file skipped")
            passed += 1
        else:
            print(f"FAIL: oversized scanned {e} events")
            failed += 1
    finally:
        try:
            big.unlink()
            big.parent.rmdir()
        except OSError:
            pass

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- section: cli-entry-point ----------
def _usage() -> str:
    return (
        "Usage:\n"
        "  toolforge_usage_detector.py scan [--days N] [--force] [--persist] [--json]\n"
        "  toolforge_usage_detector.py --self-test\n"
    )


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage(), file=sys.stderr)
        return 2
    if argv[0] == "--self-test":
        return _self_test()
    if argv[0] != "scan":
        print(_usage(), file=sys.stderr)
        return 2

    days = DEFAULT_DAYS
    force = False
    persist = False
    as_json = False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--days" and i + 1 < len(argv):
            try:
                days = int(argv[i + 1])
            except ValueError:
                print(f"invalid --days value {argv[i+1]!r}", file=sys.stderr)
                return 2
            i += 2
            continue
        if a == "--force":
            force = True
        elif a == "--persist":
            persist = True
        elif a == "--json":
            as_json = True
        else:
            print(f"unknown arg {a!r}", file=sys.stderr)
            return 2
        i += 1

    try:
        result = detect_usage(days, force=force)
    except ValueError as exc:
        print(f"usage_detector: {exc}", file=sys.stderr)
        return 2

    if persist:
        n_written, n_failed = persist_to_db(result)
        result["persisted_rows"] = n_written
        result["persisted_failed"] = n_failed

    if as_json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"\n=== ToolForge Usage Detector — last {result['window_days']} days ===")
    print(f"Transcripts scanned:  {result['transcripts_scanned']} / {result['transcripts_available']}")
    print(f"Events scanned:       {result['events_scanned']}")
    print(f"tool_use blocks:      {result['tool_uses_found']}")
    if result.get("partial"):
        print("PARTIAL: scan budget exceeded, some transcripts skipped")
    if persist:
        print(f"Persisted to DB:      {result.get('persisted_rows', 0)} rows "
              f"({result.get('persisted_failed', 0)} failed)")

    by_ns: dict[str, list[tuple[str, int]]] = {ns: [] for ns in TOOL_KEY_NAMESPACES}
    for key, count in result["counts"].items():
        ns = key.split(":", 1)[0] if ":" in key else "builtin"
        by_ns.setdefault(ns, []).append((key, count))

    for ns in ("skill", "agent", "mcp", "builtin"):
        items = by_ns.get(ns, [])
        if not items:
            continue
        print(f"\n--- {ns} ({sum(c for _, c in items)} total invocations across {len(items)} unique) ---")
        for key, count in items[:20]:
            print(f"  {key:50s}  {count:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
