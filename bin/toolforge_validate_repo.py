#!/usr/bin/env python3
"""ToolForge repo self-consistency gate — the publish/CI validation boundary.

Bidirectional check on the plugin repo itself: every declared artifact must
exist (forward), and on-disk artifacts that should be wired in but aren't are
surfaced (reverse, as warnings). Port of the autoskills validate-registry idea
adapted to ToolForge's plugin layout.

Checks (structure is discovered, not assumed — plugin.json drives it):
  (a) every hook command referenced in plugin.json points to an existing file
  (b) every commands/*.md has parseable frontmatter (no duplicate command
      names; command identity is the filename stem) and no dangling file
      reference in its frontmatter
  (c) every skills/*/SKILL.md parses with name + description in frontmatter
  (d) orphan hook .py files on disk never referenced by plugin.json -> WARN
      (listed, not fatal — some may be intentionally staged)
  (e) every bin/*.py compiles (py_compile) and every bin module exposing
      --self-test passes it
  (f) if an offline-fallback sha256 manifest exists, verify it

Output: human-readable report (default) or --json. Exit codes:
  0  clean (warnings allowed)
  1  usage error
  2  one or more failures

Stdlib only — Python 3.10+ (no hashlib.file_digest, no 3.11+ APIs).

CLI:
  toolforge_validate_repo.py [--json]
  toolforge_validate_repo.py --self-test
"""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # bin/
REPO_ROOT = HERE.parent                          # plugin repo root

# This module runs every other bin self-test in check (e); it must skip its own
# file so --self-test does not recurse into itself.
_SELF_NAME = Path(__file__).name

# Self-test timeout per sub-module (seconds). Existing self-tests are fast and
# offline; this is a guard against a future module hanging the gate.
_SELFTEST_TIMEOUT = 120

_CHUNK = 1 << 20                                  # 1 MiB sha256 read chunk


# ---------- section: result-model ----------

class Report:
    """Accumulates pass/fail/warn findings, renders human-readable or JSON."""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, check: str, status: str, msg: str) -> None:
        # status in {"ok", "fail", "warn"}
        self.checks.append({"check": check, "status": status, "msg": msg})

    def ok(self, check: str, msg: str) -> None:
        self.add(check, "ok", msg)

    def fail(self, check: str, msg: str) -> None:
        self.add(check, "fail", msg)

    def warn(self, check: str, msg: str) -> None:
        self.add(check, "warn", msg)

    @property
    def failures(self) -> int:
        return sum(1 for c in self.checks if c["status"] == "fail")

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c["status"] == "warn")

    @property
    def passes(self) -> int:
        return sum(1 for c in self.checks if c["status"] == "ok")

    def exit_code(self) -> int:
        return 2 if self.failures else 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.failures == 0,
                "passes": self.passes,
                "failures": self.failures,
                "warnings": self.warnings,
                "checks": self.checks,
            },
            indent=2,
        )

    def render(self) -> str:
        glyph = {"ok": "PASS", "fail": "FAIL", "warn": "WARN"}
        lines = ["ToolForge repo validation", "=" * 40]
        for c in self.checks:
            lines.append(f"[{glyph[c['status']]}] {c['check']}: {c['msg']}")
        lines.append("-" * 40)
        verdict = "FAILED" if self.failures else "OK"
        lines.append(
            f"{verdict} — {self.passes} passed, {self.failures} failed, "
            f"{self.warnings} warned"
        )
        return "\n".join(lines)


# ---------- section: frontmatter-parsing ----------

def _split_frontmatter(text: str) -> dict | None:
    """Return parsed YAML-ish frontmatter as a flat str->str dict, or None.

    Only handles the `key: value` shape ToolForge commands/skills actually use
    (no nested structures, no lists). A file with no leading `---` block returns
    None (distinct from {} which means an empty block was present).
    """
    if not text.startswith("---"):
        return None
    # frontmatter is delimited by the first two `---` lines
    parts = text.split("\n")
    if parts[0].strip() != "---":
        return None
    fm: dict[str, str] = {}
    for i in range(1, len(parts)):
        line = parts[i]
        if line.strip() == "---":
            return fm
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s?(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    # no closing delimiter -> treat as malformed (no frontmatter)
    return None


# match a path-like token only if it looks like a real relative file ref
_PATH_TOKEN = re.compile(r"\b([\w./\\-]+\.(?:py|md|json|txt|sh|mjs|js|yml|yaml))\b")


def _dangling_refs(fm: dict, base_dir: Path) -> list[str]:
    """Cheap dangling-reference catch: any value that names a repo file which
    does not exist on disk. Only flags tokens that resolve under REPO_ROOT and
    clearly point at a project artifact (relative, not a URL or bare word)."""
    missing: list[str] = []
    for key, value in fm.items():
        if "://" in value:                       # skip URLs
            continue
        for tok in _PATH_TOKEN.findall(value):
            ref = tok.replace("\\", "/")
            if ref.startswith(("http", "www.")):
                continue
            candidates = [base_dir / ref, REPO_ROOT / ref]
            if not any(p.exists() for p in candidates):
                missing.append(f"{key} -> {ref}")
    return missing


# ---------- section: hashing ----------

def _sha256(path: Path) -> str:
    """Chunked sha256. Avoids hashlib.file_digest (3.11+) for 3.10 support."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _sha256_lf(path: Path) -> str:
    """sha256 of the LF-normalized bytes (CRLF -> LF, stray CR -> LF).

    Manifests are committed against git-canonical (LF) content; under
    core.autocrlf=true a Windows checkout has CRLF on disk and a byte-exact
    hash would spuriously mismatch. This matches git's text=auto digest so the
    gate passes identically on Windows and Linux.
    """
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(raw).hexdigest()


# ---------- section: checks ----------

def _load_plugin_json(root: Path, report: Report) -> dict | None:
    pj = root / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        report.fail("plugin.json", f"missing at {pj.relative_to(root)}")
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        report.fail("plugin.json", f"unparseable: {exc}")
        return None
    report.ok("plugin.json", "parsed")
    return data


_HOOK_PATH = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+\.py)")


def _referenced_hook_files(plugin: dict) -> set[str]:
    """Extract every hooks/*.py path declared in plugin.json (relative to root)."""
    refs: set[str] = set()
    hooks = plugin.get("hooks", {})
    for spec_list in hooks.values():
        if not isinstance(spec_list, list):
            continue
        for spec in spec_list:
            for hook in spec.get("hooks", []) if isinstance(spec, dict) else []:
                cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                for m in _HOOK_PATH.finditer(cmd):
                    refs.add(m.group(1).replace("\\", "/"))
    return refs


def check_hooks(root: Path, plugin: dict, report: Report) -> None:
    refs = _referenced_hook_files(plugin)
    if not refs:
        report.ok("hooks", "no hook commands declared")
    for rel in sorted(refs):
        target = root / rel
        if target.is_file():
            report.ok("hooks", f"{rel} exists")
        else:
            report.fail("hooks", f"{rel} referenced in plugin.json but missing")

    # (d) orphan detection — hook .py on disk never referenced
    hooks_dir = root / "hooks"
    if hooks_dir.is_dir():
        on_disk = {
            f"hooks/{p.name}"
            for p in hooks_dir.glob("*.py")
            if p.name != "__init__.py"
        }
        for orphan in sorted(on_disk - refs):
            report.warn("hooks", f"{orphan} on disk but never referenced in plugin.json")


def check_commands(root: Path, report: Report) -> None:
    cmd_dir = root / "commands"
    if not cmd_dir.is_dir():
        report.warn("commands", "no commands/ directory")
        return
    seen: dict[str, str] = {}
    files = sorted(cmd_dir.glob("*.md"))
    if not files:
        report.warn("commands", "commands/ directory is empty")
    for md in files:
        name = md.stem                            # filename is command identity
        if name in seen:
            report.fail(
                "commands",
                f"duplicate command name '{name}' ({md.name} vs {seen[name]})",
            )
        else:
            seen[name] = md.name
        try:
            text = md.read_text(encoding="utf-8")
        except OSError as exc:
            report.fail("commands", f"{md.name} unreadable: {exc}")
            continue
        fm = _split_frontmatter(text)
        # Frontmatter is optional (some commands use a `# /name` header), but a
        # malformed opening block (starts with --- yet never closes) is a fail.
        if fm is None and text.lstrip().startswith("---"):
            report.fail("commands", f"{md.name} has an unterminated frontmatter block")
            continue
        dangling = _dangling_refs(fm or {}, cmd_dir)
        if dangling:
            report.fail(
                "commands",
                f"{md.name} frontmatter references missing file(s): {', '.join(dangling)}",
            )
        else:
            report.ok("commands", f"{md.name} valid")


def check_skills(root: Path, report: Report) -> None:
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        report.warn("skills", "no skills/ directory")
        return
    dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
    if not dirs:
        report.warn("skills", "skills/ directory has no skill folders")
    for sd in dirs:
        skill_md = sd / "SKILL.md"
        if not skill_md.is_file():
            report.fail("skills", f"{sd.name}/ has no SKILL.md")
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            report.fail("skills", f"{sd.name}/SKILL.md unreadable: {exc}")
            continue
        fm = _split_frontmatter(text)
        if fm is None:
            report.fail("skills", f"{sd.name}/SKILL.md has no parseable frontmatter")
            continue
        missing_keys = [k for k in ("name", "description") if not fm.get(k)]
        if missing_keys:
            report.fail(
                "skills",
                f"{sd.name}/SKILL.md frontmatter missing: {', '.join(missing_keys)}",
            )
            continue
        dangling = _dangling_refs(fm, sd)
        if dangling:
            report.fail(
                "skills",
                f"{sd.name}/SKILL.md references missing file(s): {', '.join(dangling)}",
            )
        else:
            report.ok("skills", f"{sd.name} valid (name='{fm['name']}')")


def _module_exposes_selftest(path: Path) -> bool:
    try:
        return "--self-test" in path.read_text(encoding="utf-8")
    except OSError:
        return False


def check_bin(root: Path, report: Report, run_selftests: bool = True) -> None:
    bin_dir = root / "bin"
    if not bin_dir.is_dir():
        report.warn("bin", "no bin/ directory")
        return
    for py in sorted(bin_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        # compile every module
        try:
            py_compile.compile(str(py), doraise=True)
        except py_compile.PyCompileError as exc:
            report.fail("bin", f"{py.name} failed py_compile: {exc.msg.strip()}")
            continue
        if py.name == _SELF_NAME:
            report.ok("bin", f"{py.name} compiles (self; self-test skipped to avoid recursion)")
            continue
        if not run_selftests or not _module_exposes_selftest(py):
            report.ok("bin", f"{py.name} compiles")
            continue
        rc, tail = _run_selftest(py)
        if rc == 0:
            report.ok("bin", f"{py.name} compiles + self-test passed")
        else:
            report.fail("bin", f"{py.name} self-test failed (exit {rc}): {tail}")


def _run_selftest(py: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [sys.executable, str(py), "--self-test"],
            capture_output=True,
            text=True,
            timeout=_SELFTEST_TIMEOUT,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {_SELFTEST_TIMEOUT}s"
    except OSError as exc:
        return 1, str(exc)
    combined = (proc.stdout + proc.stderr).strip().splitlines()
    tail = combined[-1] if combined else ""
    return proc.returncode, tail


_MANIFEST_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")


def _find_manifest(root: Path) -> Path | None:
    """Locate an offline-fallback sha256 manifest. Prefers the known location,
    falls back to scanning for any *.sha256 under the repo (excluding VCS)."""
    known = root / "fallback" / "manifest.sha256"
    if known.is_file():
        return known
    for cand in sorted(root.rglob("*.sha256")):
        if ".git" in cand.parts:
            continue
        return cand
    return None


def check_manifest(root: Path, report: Report) -> None:
    manifest = _find_manifest(root)
    if manifest is None:
        report.ok("manifest", "no offline-fallback sha256 manifest present (skipped)")
        return
    rel_manifest = _safe_rel(manifest, root)
    # manifest paths are relative to the manifest's grandparent (matches the
    # existing toolforge_verify_fallback.py convention: fallback/manifest.sha256
    # listing `fallback/backend.json`).
    base = manifest.resolve().parent.parent
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report.fail("manifest", f"{rel_manifest} unreadable: {exc}")
        return
    entries = 0
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _MANIFEST_LINE.match(line)
        if not m:
            report.fail("manifest", f"{rel_manifest}:{lineno} malformed entry")
            continue
        expected, rel = m.group(1).lower(), m.group(2).strip()
        target = (base / rel)
        # reject traversal outside base
        try:
            target.resolve().relative_to(base.resolve())
        except ValueError:
            report.fail("manifest", f"{rel} escapes manifest base dir")
            continue
        if not target.is_file():
            report.fail("manifest", f"{rel} listed in manifest but missing on disk")
            continue
        # Accept either a byte-exact match or the LF-normalized (git-canonical)
        # digest, so core.autocrlf line-ending churn doesn't trip the gate.
        if expected in (_sha256(target), _sha256_lf(target)):
            entries += 1
        else:
            actual = _sha256(target)
            report.fail(
                "manifest",
                f"{rel} hash mismatch (expected {expected[:12]}…, got {actual[:12]}…)",
            )
    if entries:
        report.ok("manifest", f"{rel_manifest}: {entries} file(s) verified")


def _safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


# ---------- section: driver ----------

def validate(root: Path, run_selftests: bool = True) -> Report:
    report = Report()
    plugin = _load_plugin_json(root, report)
    if plugin is not None:
        check_hooks(root, plugin, report)
    check_commands(root, report)
    check_skills(root, report)
    check_bin(root, report, run_selftests=run_selftests)
    check_manifest(root, report)
    return report


# ---------- section: self-test ----------

def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _make_valid_fixture(root: Path) -> None:
    """A minimal-but-valid plugin tree the validator should pass clean."""
    _write(
        root / ".claude-plugin" / "plugin.json",
        json.dumps(
            {
                "name": "fixture",
                "version": "0.0.1",
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": 'python "${CLAUDE_PLUGIN_ROOT}/hooks/router.py"',
                                }
                            ],
                        }
                    ]
                },
            }
        ),
    )
    _write(root / "hooks" / "router.py", "print('ok')\n")
    _write(
        root / "commands" / "demo.md",
        "---\ndescription: a demo command\nargument-hint: <x>\n---\n\nbody\n",
    )
    # a frontmatter-less command (mirrors real repo's bridge/profile commands)
    _write(root / "commands" / "headered.md", "# /headered\n\nbody\n")
    _write(
        root / "skills" / "demo-skill" / "SKILL.md",
        "---\nname: demo-skill\ndescription: does a demo thing\nlicense: MIT\n---\n\n# Demo\n",
    )
    # a bin module that compiles and passes its own --self-test
    _write(
        root / "bin" / "demo_mod.py",
        "import sys\n"
        "def _self_test():\n"
        "    print('OK: demo'); return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(_self_test() if '--self-test' in sys.argv else 0)\n",
    )
    # a manifest that matches an on-disk file
    payload_dir = root / "fallback"
    _write(payload_dir / "data.json", '{"x": 1}\n')
    digest = _sha256(payload_dir / "data.json")
    _write(payload_dir / "manifest.sha256", f"{digest}  fallback/data.json\n")


def _self_test() -> int:
    passed = 0
    failed = 0

    def check(desc: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            print(f"OK: {desc}")
            passed += 1
        else:
            print(f"FAIL: {desc}{(' — ' + detail) if detail else ''}")
            failed += 1

    tmp = Path(tempfile.mkdtemp(prefix="toolforge_validate_repo_test_"))
    try:
        # --- valid fixture passes clean ---
        valid = tmp / "valid"
        _make_valid_fixture(valid)
        r = validate(valid)
        check("valid fixture: 0 failures", r.failures == 0, r.render())
        check("valid fixture: exit 0", r.exit_code() == 0)
        check("valid fixture: manifest verified",
              any(c["check"] == "manifest" and c["status"] == "ok" for c in r.checks))
        check("valid fixture: bin self-test ran",
              any("self-test passed" in c["msg"] for c in r.checks))
        check("valid fixture: frontmatter-less command accepted",
              any(c["check"] == "commands" and "headered.md valid" in c["msg"]
                  for c in r.checks))
        check("valid fixture: JSON renders ok=true",
              json.loads(r.to_json())["ok"] is True)

        # --- broken hook reference fails (forward direction) ---
        broken_ref = tmp / "broken_ref"
        _make_valid_fixture(broken_ref)
        (broken_ref / "hooks" / "router.py").unlink()
        r = validate(broken_ref, run_selftests=False)
        check("broken hook ref: fails",
              r.failures >= 1 and r.exit_code() == 2)
        check("broken hook ref: blames the hook",
              any(c["check"] == "hooks" and c["status"] == "fail" for c in r.checks))

        # --- orphan hook warns, does not fail ---
        orphan = tmp / "orphan"
        _make_valid_fixture(orphan)
        _write(orphan / "hooks" / "staged.py", "print('staged')\n")
        r = validate(orphan, run_selftests=False)
        check("orphan hook: warns", r.warnings >= 1)
        check("orphan hook: does NOT fail", r.failures == 0)
        check("orphan hook: names the file",
              any("staged.py" in c["msg"] and c["status"] == "warn"
                  for c in r.checks))

        # --- skill missing description fails ---
        bad_skill = tmp / "bad_skill"
        _make_valid_fixture(bad_skill)
        _write(bad_skill / "skills" / "demo-skill" / "SKILL.md",
               "---\nname: demo-skill\n---\n\n# no description\n")
        r = validate(bad_skill, run_selftests=False)
        check("skill missing description: fails",
              any(c["check"] == "skills" and c["status"] == "fail"
                  and "description" in c["msg"] for c in r.checks))

        # --- duplicate command name fails ---
        # (filenames are unique on disk, so simulate via the parser directly)
        dup = tmp / "dup"
        _make_valid_fixture(dup)
        # craft a command whose frontmatter points at a missing file -> dangling
        _write(dup / "commands" / "dangle.md",
               "---\ndescription: see ./nope.py for details\n---\n\nbody\n")
        r = validate(dup, run_selftests=False)
        check("dangling frontmatter ref: fails",
              any(c["check"] == "commands" and c["status"] == "fail"
                  and "nope.py" in c["msg"] for c in r.checks))

        # --- tampered manifest fails (reverse: declared hash != disk) ---
        tampered = tmp / "tampered"
        _make_valid_fixture(tampered)
        (tampered / "fallback" / "data.json").write_text('{"x": 999}\n',
                                                          encoding="utf-8")
        r = validate(tampered, run_selftests=False)
        check("tampered manifest: hash mismatch fails",
              any(c["check"] == "manifest" and c["status"] == "fail"
                  and "mismatch" in c["msg"] for c in r.checks))

        # --- bad bin self-test fails check (e) ---
        bad_bin = tmp / "bad_bin"
        _make_valid_fixture(bad_bin)
        _write(bad_bin / "bin" / "broken_mod.py",
               "import sys\n"
               "if __name__ == '__main__':\n"
               "    sys.exit(1 if '--self-test' in sys.argv else 0)\n")
        r = validate(bad_bin, run_selftests=True)
        check("failing bin self-test: fails",
              any(c["check"] == "bin" and c["status"] == "fail"
                  and "broken_mod.py" in c["msg"] for c in r.checks))

        # --- syntactically broken bin fails py_compile ---
        nocompile = tmp / "nocompile"
        _make_valid_fixture(nocompile)
        _write(nocompile / "bin" / "syntaxerr.py", "def (:\n")
        r = validate(nocompile, run_selftests=False)
        check("uncompilable bin: fails py_compile",
              any(c["check"] == "bin" and c["status"] == "fail"
                  and "syntaxerr.py" in c["msg"] for c in r.checks))

        # --- missing plugin.json fails ---
        nopj = tmp / "nopj"
        (nopj / "commands").mkdir(parents=True)
        r = validate(nopj, run_selftests=False)
        check("missing plugin.json: fails",
              any(c["check"] == "plugin.json" and c["status"] == "fail"
                  for c in r.checks))

    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"--- self-test: {passed} passed, {failed} failed ---")
    return 0 if failed == 0 else 1


# ---------- section: cli ----------

_USAGE = (
    "Usage: toolforge_validate_repo.py [--json]\n"
    "       toolforge_validate_repo.py --self-test"
)


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return _self_test()
    as_json = "--json" in argv
    leftover = [a for a in argv if a not in ("--json",)]
    if leftover:
        print(f"Unknown argument(s): {' '.join(leftover)}\n{_USAGE}", file=sys.stderr)
        return 1
    report = validate(REPO_ROOT)
    if as_json:
        print(report.to_json())
    else:
        print(report.render())
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
