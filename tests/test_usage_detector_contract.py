#!/usr/bin/env python3
"""Contract tests for toolforge_usage_detector.

Guards against Anthropic transcript-schema drift. The fixture at
fixtures/sample_transcript.jsonl encodes every shape we currently rely on
(tool_use blocks, plugin-namespaced skill names, mcp__server__tool, Task /
Agent subagent shape, bad JSON lines, missing fields, old timestamps,
forward-compat unknown fields, tool_use in user messages we must ignore).

If Anthropic changes the wire format, run these tests — failures point at
exactly which detector branch needs an update.

Run: python -m unittest toolforge.tests.test_usage_detector_contract -v
or:  python toolforge/tests/test_usage_detector_contract.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import toolforge_usage_detector as ud  # noqa: E402

FIXTURE = HERE / "fixtures" / "sample_transcript.jsonl"


def _materialize_fixture(tmpdir: Path) -> Path:
    """Substitute timestamp placeholders with real ISO 8601 strings.

    __RECENT__ -> 5 minutes ago (well inside any reasonable cutoff)
    __OLD__    -> 400 days ago (always outside cutoff for days<=365)
    """
    raw = FIXTURE.read_text(encoding="utf-8")
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = raw.replace("__RECENT__", recent).replace("__OLD__", old)
    dst = tmpdir / "session.jsonl"
    dst.write_text(out, encoding="utf-8")
    return dst


class ClassifyContract(unittest.TestCase):
    """Each branch of _classify against a known input."""

    def test_skill_namespace_lowercased(self) -> None:
        k = ud._classify({"type": "tool_use", "name": "Skill",
                          "input": {"skill": "GSAP-React"}})
        self.assertEqual(k, "skill:gsap-react")

    def test_skill_plugin_namespaced_colon_to_slash(self) -> None:
        # Plugin-scoped skills carry a colon. tool_key must stay parse-able
        # against TOOL_KEY_RE (which uses colon as namespace separator), so
        # the inner colon maps to '/'.
        k = ud._classify({"type": "tool_use", "name": "Skill",
                          "input": {"skill": "skill-creator:skill-creator"}})
        self.assertEqual(k, "skill:skill-creator/skill-creator")

    def test_skill_empty_string_returns_none(self) -> None:
        self.assertIsNone(ud._classify({"type": "tool_use", "name": "Skill",
                                        "input": {"skill": ""}}))

    def test_skill_missing_input_returns_none(self) -> None:
        self.assertIsNone(ud._classify({"type": "tool_use", "name": "Skill"}))

    def test_task_subagent(self) -> None:
        k = ud._classify({"type": "tool_use", "name": "Task",
                          "input": {"subagent_type": "Explore"}})
        self.assertEqual(k, "agent:explore")

    def test_agent_alias(self) -> None:
        # Some transcripts emit "Agent" as the tool name (older harness shape).
        k = ud._classify({"type": "tool_use", "name": "Agent",
                          "input": {"subagent_type": "general-purpose"}})
        self.assertEqual(k, "agent:general-purpose")

    def test_mcp_server_extracted(self) -> None:
        k = ud._classify({
            "type": "tool_use",
            "name": "mcp__plugin_github_github__add_issue_comment",
        })
        self.assertEqual(k, "mcp:plugin_github_github")

    def test_mcp_with_hyphenated_tool_segment(self) -> None:
        # tool segment with `-` (like query-docs) must not confuse server parsing.
        k = ud._classify({"type": "tool_use",
                          "name": "mcp__plugin_context7_context7__query-docs"})
        self.assertEqual(k, "mcp:plugin_context7_context7")

    def test_builtin_lowercase(self) -> None:
        for raw, want in (("Edit", "builtin:edit"),
                          ("Bash", "builtin:bash"),
                          ("WebFetch", "builtin:webfetch")):
            self.assertEqual(ud._classify({"type": "tool_use", "name": raw}), want)

    def test_missing_name_returns_none(self) -> None:
        self.assertIsNone(ud._classify({"type": "tool_use"}))

    def test_forward_compat_unknown_input_keys_ignored(self) -> None:
        # If Anthropic adds new keys to a Skill input, the detector must
        # still extract the skill name and ignore the extras.
        k = ud._classify({"type": "tool_use", "name": "Skill",
                          "input": {"skill": "future-skill",
                                    "new_anthropic_field": "ignored"}})
        self.assertEqual(k, "skill:future-skill")


class ScanContract(unittest.TestCase):
    """End-to-end scan over the fixture."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="toolforge_contract_"))
        self.path = _materialize_fixture(self.tmpdir)

    def tearDown(self) -> None:
        try:
            for p in self.tmpdir.rglob("*"):
                if p.is_file():
                    p.unlink()
            for d in sorted(self.tmpdir.rglob("*"), reverse=True):
                if d.is_dir():
                    d.rmdir()
            self.tmpdir.rmdir()
        except OSError:
            pass

    def test_counts_expected_classifications(self) -> None:
        # Cut at 30 days back so the synthetic __OLD__ row (400d) is excluded
        # and only __RECENT__ assistant events count.
        import time
        cutoff = time.time() - 30 * 86400
        counts: Counter = Counter()
        last_used: dict[str, float] = {}
        events, uses = ud._scan_one(str(self.path), cutoff, counts, last_used)
        self.assertEqual(counts["builtin:edit"], 2,
                         f"expected 2 Edit (1 in mixed message, 1 in 4-tool message); got {counts['builtin:edit']}")
        self.assertEqual(counts["builtin:bash"], 1)
        self.assertEqual(counts["builtin:grep"], 1)
        self.assertEqual(counts["builtin:read"], 1)
        self.assertEqual(counts["skill:gsap-react"], 1)
        self.assertEqual(counts["skill:skill-creator/skill-creator"], 1)
        self.assertEqual(counts["skill:future-skill"], 1)
        self.assertEqual(counts["agent:explore"], 1)
        self.assertEqual(counts["agent:general-purpose"], 1)
        self.assertEqual(counts["mcp:plugin_github_github"], 1)
        self.assertEqual(counts["mcp:plugin_context7_context7"], 1)
        self.assertGreater(events, 0)
        self.assertGreater(uses, 0)

    def test_old_timestamp_skipped_against_cutoff(self) -> None:
        # Use a cutoff equivalent to "30 days ago": __OLD__ row (400 days back)
        # must NOT be counted, __RECENT__ rows must be.
        import time
        cutoff_epoch = time.time() - 30 * 86400
        counts: Counter = Counter()
        last_used: dict[str, float] = {}
        ud._scan_one(str(self.path), cutoff_epoch, counts, last_used)
        # The __OLD__ row contains an Edit. With the cutoff, we expect 2 Edits
        # (only the __RECENT__ ones). If the cutoff is being ignored, we'd see 3.
        self.assertEqual(counts.get("builtin:edit", 0), 2)

    def test_tool_use_in_user_message_ignored(self) -> None:
        # Fixture last line is a "user" event with a tool_use block — privacy
        # boundary: we MUST NOT count it. Counts come only from assistant emits.
        # Cut at 30d back to exclude __OLD__; expect exactly 2 Edits from the two
        # recent assistant events that contain Edit. If user-msg leaked through
        # we'd see 3.
        import time
        cutoff = time.time() - 30 * 86400
        counts: Counter = Counter()
        last_used: dict[str, float] = {}
        ud._scan_one(str(self.path), cutoff, counts, last_used)
        self.assertEqual(counts.get("builtin:edit", 0), 2)

    def test_bad_json_line_does_not_abort_scan(self) -> None:
        counts: Counter = Counter()
        last_used: dict[str, float] = {}
        events, uses = ud._scan_one(str(self.path), 0.0, counts, last_used)
        # If the bad line aborted the scan, we'd miss everything after it
        # (Bash/Grep/Read/Edit + several others). Sum must exceed 8.
        total = sum(counts.values())
        self.assertGreater(total, 8,
                           f"bad-json line short-circuited the scan; total={total}")

    def test_last_used_recorded_for_each_key(self) -> None:
        counts: Counter = Counter()
        last_used: dict[str, float] = {}
        ud._scan_one(str(self.path), 0.0, counts, last_used)
        for key in ("builtin:edit", "skill:gsap-react", "mcp:plugin_github_github"):
            self.assertIn(key, last_used)
            self.assertGreater(last_used[key], 0.0)


class ToolKeyShapeContract(unittest.TestCase):
    """Every emitted tool_key must satisfy toolforge_db.TOOL_KEY_RE."""

    def test_every_emitted_key_matches_db_regex(self) -> None:
        # Import the DB regex at test-time so a schema bump that loosens or
        # tightens the rule will be caught here.
        sys.path.insert(0, str(BIN))
        import toolforge_db
        regex: re.Pattern = toolforge_db.TOOL_KEY_RE
        tmpdir = Path(tempfile.mkdtemp(prefix="toolforge_keyshape_"))
        try:
            path = _materialize_fixture(tmpdir)
            counts: Counter = Counter()
            last_used: dict[str, float] = {}
            ud._scan_one(str(path), 0.0, counts, last_used)
            for key in counts:
                self.assertRegex(key, regex,
                                 f"emitted tool_key {key!r} fails DB regex")
        finally:
            try:
                for p in tmpdir.rglob("*"):
                    if p.is_file():
                        p.unlink()
                tmpdir.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
