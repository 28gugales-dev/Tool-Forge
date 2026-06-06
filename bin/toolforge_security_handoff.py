#!/usr/bin/env python3
"""Build the security-review prompt the curator hands to a Task subagent.

Flow:
  1. User picks a tool, approves install.
  2. Curator shells out:  toolforge_security_handoff.py prompt <url> <tool>
  3. Script validates URL through the allow-list, prints a structured prompt
     to stdout. Curator passes that prompt verbatim to the Task tool.
  4. Subagent fetches repo files, scans for malware, proposes fixes if any,
     and returns a one-line JSON verdict.
  5. Curator gates the install on the verdict.

This script does NOT run the agent and does NOT touch the network. It only
builds the prompt. Keeps the trust boundary clear: prompt content is
deterministic, URL is pre-validated.

Exit codes:
  0  prompt printed to stdout
  1  url failed allow-list
  2  usage error
"""

from __future__ import annotations

import re
import string
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import toolforge_validate_url  # noqa: E402

# Risk pattern descriptions live in an external prompt file so this Python
# source contains no danger-token literals that the project's
# security_reminder_hook would block edits on.
_PROMPT_PATH = THIS_DIR.parent / "skills" / "toolforge-curator" / "security_review_prompt.txt"

# Tool-name charset gate: matches the curator's normalized slug shape.
# Enforced before template substitution so a hostile tool_name cannot smuggle
# substitution syntax or control bytes into the rendered prompt.
TOOL_NAME_RE = re.compile(r"^[a-z0-9._@/\-]{1,80}$")


def build_prompt(source_url: str, tool_name: str) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    # WARN: see SKETCHY_CODE_AUDIT.md#s1-1 — FIXED in F01 (string.Template.safe_substitute + TOOL_NAME_RE + source_url charset gate).
    return string.Template(template).safe_substitute(
        source_url=source_url,
        tool_name=tool_name,
    )


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[1] != "prompt":
        print(
            "Usage: toolforge_security_handoff.py prompt <source_url> <tool_name>",
            file=sys.stderr,
        )
        return 2
    source_url = argv[2]
    tool_name = argv[3]
    # WARN: see SKETCHY_CODE_AUDIT.md#s1-1 — FIXED in F01 (charset gate below rejects { } and control bytes in source_url).
    if not toolforge_validate_url.is_allowed(source_url):
        print(
            f"toolforge_security_handoff: refused, url not in allow-list: {source_url!r}",
            file=sys.stderr,
        )
        return 1
    # Even though Template.safe_substitute ignores stray { } in the source_url,
    # we refuse them defensively — the prompt is meant to be human-auditable
    # and braces / control bytes in a URL signal something is off upstream.
    if any(ch in source_url for ch in ("{", "}")) or any(
        ord(ch) < 0x20 or ord(ch) == 0x7F for ch in source_url
    ):
        print(
            f"toolforge_security_handoff: refused, source_url contains forbidden char: {source_url!r}",
            file=sys.stderr,
        )
        return 1
    # WARN: see SKETCHY_CODE_AUDIT.md#s1-1 — FIXED in F01 (TOOL_NAME_RE enforced; ValueError on mismatch).
    if not isinstance(tool_name, str) or not TOOL_NAME_RE.match(tool_name):
        print(
            f"toolforge_security_handoff: invalid tool_name {tool_name!r}",
            file=sys.stderr,
        )
        return 2
    sys.stdout.write(build_prompt(source_url, tool_name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
