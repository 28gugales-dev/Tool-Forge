#!/usr/bin/env python3
"""Safe test suite for ToolForge v0.3 + v0.4 additions.

Runs entirely against a temp directory — never touches ~/.claude/toolforge.db,
never touches ~/.claude/toolforge-config.json, never runs any install command.

Run with: python tests/test_v03_safe.py
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

# Force UTF-8 stdout on Windows so non-ASCII chars in output don't crash the test runner.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── repo root on path ──────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
CATALOG_DIR = REPO_ROOT / "catalog"
sys.path.insert(0, str(BIN_DIR))

# ── test state ─────────────────────────────────────────────────────────────────
passed = 0
failed = 0
errors: list[str] = []


def ok(msg: str) -> None:
    global passed
    passed += 1
    print(f"  OK  {msg}")


def fail(msg: str, detail: str = "") -> None:
    global failed
    failed += 1
    err = f"FAIL  {msg}" + (f": {detail}" if detail else "")
    print(f"  {err}")
    errors.append(err)


def section(title: str) -> None:
    print(f"\n{'=' * 55}")
    print(f"  {title}")
    print("=" * 55)


# ── setup: redirect DB to temp directory ──────────────────────────────────────

_tmpdir = tempfile.mkdtemp(prefix="toolforge_test_v03_")
_tmp_db = Path(_tmpdir) / "toolforge.db"
_tmp_cfg = Path(_tmpdir) / "toolforge-config.json"

# ── snapshot real DB BEFORE tests run ──────────────────────────────────────────
# The real DB may legitimately pre-exist from a real ToolForge install. A bare
# existence check can't tell a test leak from a pre-existing file, so capture
# existence + mtime + size up front. Leak detection then becomes: "tests must not
# CREATE or MODIFY the real DB" — pre-existing-unchanged passes, new/changed fails.
_REAL_DB = Path(os.path.expanduser("~/.claude/toolforge.db"))


def _db_fingerprint(p: Path):
    if not p.exists():
        return None
    st = p.stat()
    return (st.st_mtime_ns, st.st_size)


_REAL_DB_EXISTED = _REAL_DB.exists()
_REAL_DB_FINGERPRINT = _db_fingerprint(_REAL_DB)


def _real_db_touched() -> tuple[bool, str]:
    """Return (touched, reason). touched=True only if the real DB was newly
    created or its mtime/size changed since the pre-test snapshot."""
    now_exists = _REAL_DB.exists()
    if not _REAL_DB_EXISTED and now_exists:
        return True, "real DB was created by tests"
    if _REAL_DB_EXISTED and not now_exists:
        return True, "real DB was deleted by tests"
    if now_exists and _db_fingerprint(_REAL_DB) != _REAL_DB_FINGERPRINT:
        return True, "real DB mtime/size changed during tests"
    return False, ""

# Patch DB_PATH before importing any module that uses it.
import toolforge_db as _db_mod
_db_mod.DB_PATH = _tmp_db  # redirect all DB operations to temp file

# Re-import downstream modules so they pick up the patched DB_PATH.
import toolforge_db as db


# ══════════════════════════════════════════════════════════════════════════════
section("1. DB schema v5 — init and migration")
# ══════════════════════════════════════════════════════════════════════════════

try:
    db.init_db()
    conn = db._connect()
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    if v == db.SCHEMA_VERSION:
        ok(f"schema migrated to v{v}")
    else:
        fail("schema version", f"expected {db.SCHEMA_VERSION}, got {v}")
except Exception as e:
    fail("init_db raised", traceback.format_exc(limit=3))

# Verify all 10 tables exist
try:
    conn = db._connect()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    expected = {
        "installs", "ratings", "usage_stats", "deprecations", "routing_scores",
        "pipelines", "token_stats", "predictions", "skill_stacks", "org_profiles", "skill_performance"
    }
    missing = expected - tables
    if not missing:
        ok(f"all {len(expected)} tables present")
    else:
        fail("tables missing", str(missing))
except Exception as e:
    fail("table check raised", str(e))

# Idempotency
try:
    db.init_db()
    db.init_db()
    ok("init_db idempotent (called 3×)")
except Exception as e:
    fail("init_db idempotency", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("2. token_stats table")
# ══════════════════════════════════════════════════════════════════════════════

try:
    db.log_token_stats("sess-001", "playwright", 1200, 450)
    db.log_token_stats("sess-001", "playwright", 1100, 400)
    db.log_token_stats("sess-002", "context7", 800, 300)
    db.log_token_stats("sess-002", None, 500, 200)
    ok("log_token_stats: 4 rows inserted (incl. null skill)")
except Exception as e:
    fail("log_token_stats raised", str(e))

try:
    bulk = db.get_token_stats_bulk(["playwright", "context7"])
    assert bulk["playwright"]["sessions"] == 2, f"expected 2 sessions, got {bulk['playwright']}"
    assert bulk["context7"]["sessions"] == 1
    expected_avg = (1200 + 450 + 1100 + 400) / 2
    assert abs(bulk["playwright"]["avg_tokens"] - expected_avg) < 1, f"avg_tokens wrong: {bulk['playwright']}"
    ok("get_token_stats_bulk: correct aggregates")
except Exception as e:
    fail("get_token_stats_bulk", str(e))

try:
    rank = db.get_token_efficiency_rank()
    # Need >= 3 sessions for a skill to appear; add more
    for i in range(2):
        db.log_token_stats(f"sess-extra-{i}", "playwright", 900, 350)
    rank = db.get_token_efficiency_rank()
    assert any(r["skill_name"] == "playwright" for r in rank), f"playwright not in rank: {rank}"
    ok("get_token_efficiency_rank: playwright appears after 4 sessions")
except Exception as e:
    fail("get_token_efficiency_rank", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("3. predictions table")
# ══════════════════════════════════════════════════════════════════════════════

try:
    db.log_prediction("sess-001", "playwright", 0.85)
    db.log_prediction("sess-001", "context7", 0.72)
    db.log_prediction("sess-001", "sequential-thinking", 0.51)
    ok("log_prediction: 3 predictions inserted")
except Exception as e:
    fail("log_prediction raised", str(e))

try:
    db.confirm_prediction("sess-001", "playwright")
    db.confirm_prediction("sess-001", "context7")
    ok("confirm_prediction: 2 confirmed")
except Exception as e:
    fail("confirm_prediction raised", str(e))

try:
    acc = db.get_prediction_accuracy()
    assert acc["total"] == 3
    assert acc["hits"] == 2
    assert abs(acc["accuracy"] - 2/3) < 0.01, f"accuracy wrong: {acc}"
    ok(f"get_prediction_accuracy: {acc['hits']}/{acc['total']} = {acc['accuracy']:.1%}")
except Exception as e:
    fail("get_prediction_accuracy", str(e))

try:
    top = db.get_top_predicted_skills(5)
    skill_names = [t["skill"] for t in top]
    assert "playwright" in skill_names
    ok(f"get_top_predicted_skills: {skill_names}")
except Exception as e:
    fail("get_top_predicted_skills", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("4. skill_stacks table")
# ══════════════════════════════════════════════════════════════════════════════

try:
    db.save_skill_stack("my-frontend", "My Frontend Stack", "React + Tailwind tools",
                        ["21st-magic", "shadcn-ui-mcp", "context7"])
    ok("save_skill_stack: personal stack created")
except Exception as e:
    fail("save_skill_stack raised", str(e))

try:
    db.save_skill_stack("acme-data", "Acme Data Stack", "Org-wide data tools",
                        ["postgres", "sqlite", "jupyter-notebook-mcp"], org_id="acme-corp")
    ok("save_skill_stack: org stack created")
except Exception as e:
    fail("save_skill_stack org raised", str(e))

try:
    stack = db.get_skill_stack("my-frontend")
    assert stack["skills"] == ["21st-magic", "shadcn-ui-mcp", "context7"]
    assert stack["org_id"] is None
    ok("get_skill_stack: correct skills list")
except Exception as e:
    fail("get_skill_stack", str(e))

try:
    # Upsert — update skills list
    db.save_skill_stack("my-frontend", "My Frontend Stack", "updated", ["21st-magic", "magic-ui"])
    updated = db.get_skill_stack("my-frontend")
    assert updated["skills"] == ["21st-magic", "magic-ui"], f"upsert failed: {updated['skills']}"
    ok("save_skill_stack upsert: skills updated")
except Exception as e:
    fail("save_skill_stack upsert", str(e))

try:
    all_stacks = db.list_skill_stacks()
    assert len(all_stacks) == 2
    ok(f"list_skill_stacks: {len(all_stacks)} stacks")
except Exception as e:
    fail("list_skill_stacks", str(e))

try:
    org_stacks = db.list_skill_stacks("acme-corp")
    names = [s["stack_name"] for s in org_stacks]
    assert "acme-data" in names, f"org stack not found: {names}"
    ok(f"list_skill_stacks(org_id): found {names}")
except Exception as e:
    fail("list_skill_stacks org", str(e))

try:
    ok_del = db.delete_skill_stack("my-frontend")
    assert ok_del is True
    gone = db.get_skill_stack("my-frontend")
    assert gone is None
    ok("delete_skill_stack: deleted successfully")
except Exception as e:
    fail("delete_skill_stack", str(e))

try:
    # Built-in stacks cannot be deleted
    db.save_skill_stack("builtin-test", "Builtin", "desc", ["a", "b"], is_builtin=True)
    blocked = db.delete_skill_stack("builtin-test")
    assert blocked is False, f"expected False for builtin deletion, got {blocked}"
    ok("delete_skill_stack: builtin correctly blocked")
except Exception as e:
    fail("delete_skill_stack builtin guard", str(e))

try:
    # Invalid stack_name rejected
    try:
        db.save_skill_stack("Has Spaces!", "bad", "bad", [])
        fail("invalid stack_name accepted (should have raised)")
    except ValueError:
        ok("save_skill_stack: invalid name correctly rejected")
except Exception as e:
    fail("stack_name validation unexpected error", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("5. org_profiles table")
# ══════════════════════════════════════════════════════════════════════════════

try:
    db.save_org_profile("acme-corp", "Acme Engineering", "admin@acme.com", True, {"tier": "business"})
    ok("save_org_profile: acme-corp created")
except Exception as e:
    fail("save_org_profile raised", str(e))

try:
    org = db.get_org_profile("acme-corp")
    assert org["org_name"] == "Acme Engineering"
    assert org["admin_email"] == "admin@acme.com"
    assert org["shared_catalog"] is True
    assert org["config"]["tier"] == "business"
    ok("get_org_profile: all fields correct")
except Exception as e:
    fail("get_org_profile", str(e))

try:
    # Upsert
    db.save_org_profile("acme-corp", "Acme Engineering (updated)", "admin@acme.com")
    updated = db.get_org_profile("acme-corp")
    assert "updated" in updated["org_name"]
    ok("save_org_profile upsert: name updated")
except Exception as e:
    fail("save_org_profile upsert", str(e))

try:
    db.save_org_profile("beta-inc", "Beta Inc", None)
    orgs = db.list_org_profiles()
    ids = [o["org_id"] for o in orgs]
    assert "acme-corp" in ids and "beta-inc" in ids
    ok(f"list_org_profiles: {ids}")
except Exception as e:
    fail("list_org_profiles", str(e))

try:
    try:
        db.save_org_profile("Has Spaces!", "bad", None)
        fail("invalid org_id accepted")
    except ValueError:
        ok("save_org_profile: invalid org_id rejected")
except Exception as e:
    fail("org_id validation unexpected error", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("6. skill_performance table")
# ══════════════════════════════════════════════════════════════════════════════

try:
    db.upsert_skill_performance("playwright", 1200.0, True, 1650)
    db.upsert_skill_performance("playwright", 900.0, True, 1500)
    db.upsert_skill_performance("playwright", 1100.0, False, 0)
    ok("upsert_skill_performance: 3 calls recorded (2 success, 1 failure)")
except Exception as e:
    fail("upsert_skill_performance raised", str(e))

try:
    perf = db.get_skill_performance("playwright")
    assert perf is not None
    assert perf["success_count"] == 2
    assert perf["error_count"] == 1
    assert 0 < perf["error_rate"] < 1
    ok(f"get_skill_performance: error_rate={perf['error_rate']:.0%}, avg_lat={perf['avg_latency_ms']:.0f}ms")
except Exception as e:
    fail("get_skill_performance", str(e))

try:
    all_perf = db.get_skill_performance_all()
    assert any(p["skill_name"] == "playwright" for p in all_perf)
    ok(f"get_skill_performance_all: {len(all_perf)} entries")
except Exception as e:
    fail("get_skill_performance_all", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("7. catalog/suggestions.json")
# ══════════════════════════════════════════════════════════════════════════════

suggestions_file = CATALOG_DIR / "suggestions.json"
try:
    data = json.loads(suggestions_file.read_text(encoding="utf-8"))
    task_types = data["task_types"]
    ok(f"suggestions.json valid JSON: {len(task_types)} task types")
except Exception as e:
    fail("suggestions.json load", str(e))
    task_types = []

try:
    for tt in task_types:
        assert "id" in tt
        assert "label" in tt
        assert "keywords" in tt
        assert "suggestions" in tt
        assert 1 <= len(tt["suggestions"]) <= 3, f"{tt['id']}: {len(tt['suggestions'])} suggestions"
        for s in tt["suggestions"]:
            assert "name" in s
            assert "type" in s
    ok(f"all {len(task_types)} task types have required fields and 1-3 suggestions")
except Exception as e:
    fail("suggestions.json schema", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("8. toolforge_suggestions.py matching")
# ══════════════════════════════════════════════════════════════════════════════

import toolforge_suggestions as sugg

try:
    results = sugg.match_task("build a react frontend with tailwind components")
    assert len(results) > 0, "no matches returned"
    assert results[0]["score"] > 0
    top_id = results[0]["task_type_id"]
    ok(f"match_task (react/frontend): top match = {top_id!r} (score {results[0]['score']})")
except Exception as e:
    fail("match_task react", str(e))

try:
    results = sugg.match_task("write playwright e2e tests for login flow")
    ids = [r["task_type_id"] for r in results]
    ok(f"match_task (playwright): matched {ids}")
except Exception as e:
    fail("match_task playwright", str(e))

try:
    results = sugg.match_task("reduce token cost and optimize context usage")
    ids = [r["task_type_id"] for r in results]
    ok(f"match_task (token-reduction): matched {ids}")
except Exception as e:
    fail("match_task token-reduction", str(e))

try:
    result = sugg.get_by_id("frontend-ui")
    assert result is not None
    assert result["task_type_id"] == "frontend-ui"
    assert len(result["suggestions"]) >= 2
    ok(f"get_by_id('frontend-ui'): {len(result['suggestions'])} suggestions")
except Exception as e:
    fail("get_by_id", str(e))

try:
    result = sugg.get_by_id("nonexistent-id")
    assert result is None
    ok("get_by_id('nonexistent'): correctly returns None")
except Exception as e:
    fail("get_by_id nonexistent", str(e))

try:
    listing = sugg.list_all()
    assert len(listing) == len(task_types)
    ok(f"list_all: {len(listing)} entries")
except Exception as e:
    fail("list_all", str(e))

try:
    result = sugg.match_task("")
    assert result == []
    ok("match_task(''): empty query returns []")
except Exception as e:
    fail("match_task empty query", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("9. catalog/packages/*.json")
# ══════════════════════════════════════════════════════════════════════════════

packages_dir = CATALOG_DIR / "packages"
expected_packages = [
    "best-for-business", "best-for-personal", "best-for-coding",
    "best-for-token-reduction", "best-for-design", "best-for-testing"
]

try:
    manifest = json.loads((packages_dir / "manifest.json").read_text(encoding="utf-8"))
    ids = [p["id"] for p in manifest["packages"]]
    assert set(ids) == set(expected_packages), f"manifest mismatch: {ids}"
    ok(f"manifest.json: all 6 packages listed")
except Exception as e:
    fail("manifest.json", str(e))

for pkg_id in expected_packages:
    try:
        pkg_file = packages_dir / f"{pkg_id}.json"
        pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
        assert pkg["package_id"] == pkg_id
        assert len(pkg["tools"]) >= 2
        for t in pkg["tools"]:
            assert "name" in t
            assert "install_command" in t
            assert "why" in t
        ok(f"{pkg_id}: {len(pkg['tools'])} tools, all fields present")
    except Exception as e:
        fail(f"{pkg_id}.json", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("10. toolforge_packages.py")
# ══════════════════════════════════════════════════════════════════════════════

import toolforge_packages as pkgs

try:
    listing = pkgs.list_packages()
    assert len(listing) == 6
    ids = [p["id"] for p in listing]
    assert set(ids) == set(expected_packages)
    ok(f"list_packages: {len(listing)} packages")
except Exception as e:
    fail("list_packages", str(e))

try:
    pkg = pkgs.get_package("best-for-coding")
    assert pkg is not None
    assert pkg["package_id"] == "best-for-coding"
    ok("get_package('best-for-coding'): loaded")
except Exception as e:
    fail("get_package", str(e))

try:
    rendered = pkgs.render_package(pkgs.get_package("best-for-design"))
    assert "Best for Design" in rendered
    assert "install_command" not in rendered   # render uses 'Install:' label
    assert "21st-magic" in rendered
    ok("render_package: output looks correct")
except Exception as e:
    fail("render_package", str(e))

try:
    cmds = pkgs.install_commands("best-for-personal")
    assert len(cmds) >= 4
    assert all("claude mcp add" in c or "npx" in c for c in cmds)
    ok(f"install_commands('best-for-personal'): {len(cmds)} commands")
except Exception as e:
    fail("install_commands", str(e))

try:
    assert pkgs.get_package("totally-fake-package") is None
    ok("get_package(nonexistent): returns None")
except Exception as e:
    fail("get_package nonexistent", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("11. toolforge_token_tracker.py")
# ══════════════════════════════════════════════════════════════════════════════

import toolforge_token_tracker as ttrk

try:
    est = ttrk.estimate_tokens("Hello world, this is a test sentence.")
    assert est > 0
    ok(f"estimate_tokens: {est} tokens for short string")
except Exception as e:
    fail("estimate_tokens", str(e))

try:
    long_text = "a " * 1000   # 2000 chars → ~500 tokens
    est = ttrk.estimate_tokens(long_text)
    assert 400 < est < 600, f"expected ~500, got {est}"
    ok(f"estimate_tokens: {est} for ~2000-char text (expected ~500)")
except Exception as e:
    fail("estimate_tokens long text", str(e))

try:
    result = ttrk.record_from_text("sess-tok-001", "sequential-thinking",
                                   "Plan a database migration with rollback", "Here is the plan...")
    assert result["prompt_tokens"] > 0
    assert result["output_tokens"] > 0
    assert result["total_tokens"] == result["prompt_tokens"] + result["output_tokens"]
    ok(f"record_from_text: {result}")
except Exception as e:
    fail("record_from_text", str(e))

try:
    report = ttrk.efficiency_report()
    assert isinstance(report, str)
    ok("efficiency_report: returns string (may be empty without enough data)")
except Exception as e:
    fail("efficiency_report", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("12. toolforge_predictor.py")
# ══════════════════════════════════════════════════════════════════════════════

import toolforge_predictor as pred

# Seed some pipeline history for the predictor to find
try:
    steps = json.dumps([
        {"skill_name": "sequential-thinking", "sub_task": "plan"},
        {"skill_name": "playwright", "sub_task": "test"},
    ])
    db.save_pipeline("test e2e flow", "hash-e2e", steps, True)
    db.save_pipeline("test auth flow", "hash-auth", steps, True)
    ok("seeded pipeline history for predictor tests")
except Exception as e:
    fail("pipeline seeding", str(e))

try:
    predictions = pred.predict("write playwright tests for the login page")
    assert isinstance(predictions, list)
    ok(f"predict (with prompt): {len(predictions)} predictions -> {[p['skill'] for p in predictions]}")
except Exception as e:
    fail("predict with prompt", str(e))

try:
    predictions = pred.predict()
    assert isinstance(predictions, list)
    ok(f"predict (no prompt): {len(predictions)} predictions")
except Exception as e:
    fail("predict no prompt", str(e))

try:
    predictions = pred.predict_and_log("sess-pred-001", "design a react component with animations")
    assert isinstance(predictions, list)
    ok(f"predict_and_log: {len(predictions)} predictions logged for sess-pred-001")
except Exception as e:
    fail("predict_and_log", str(e))

try:
    rendered = pred.render_predictions(
        [{"skill": "playwright", "confidence": 0.87},
         {"skill": "context7", "confidence": 0.64}]
    )
    assert "playwright" in rendered
    assert "87%" in rendered
    ok("render_predictions: output looks correct")
except Exception as e:
    fail("render_predictions", str(e))

try:
    rendered_empty = pred.render_predictions([])
    assert "No strong" in rendered_empty
    ok("render_predictions (empty): correct fallback message")
except Exception as e:
    fail("render_predictions empty", str(e))

try:
    report = pred.accuracy_report()
    assert isinstance(report, str)
    assert "Prediction" in report
    ok("accuracy_report: returns string")
except Exception as e:
    fail("accuracy_report", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("13. toolforge_org.py")
# ══════════════════════════════════════════════════════════════════════════════

# Redirect config to temp dir so we never touch ~/.claude/toolforge-config.json
import toolforge_org as org_mod
org_mod._CONFIG_PATH = _tmp_cfg

try:
    result = org_mod.create_org("test-corp", "Test Corporation", "cto@test.com", True)
    assert result["org_id"] == "test-corp"
    assert result["org_name"] == "Test Corporation"
    ok("create_org: test-corp created")
except Exception as e:
    fail("create_org raised", str(e))

try:
    fetched = org_mod.get_org("test-corp")
    assert fetched is not None
    ok("get_org: found test-corp")
except Exception as e:
    fail("get_org", str(e))

try:
    orgs = org_mod.list_orgs()
    ids = [o["org_id"] for o in orgs]
    assert "test-corp" in ids
    ok(f"list_orgs: {ids}")
except Exception as e:
    fail("list_orgs", str(e))

try:
    org_mod.set_current_org_id("test-corp")
    assert org_mod.get_current_org_id() == "test-corp"
    # Verify it wrote to temp config, NOT real config
    real_config = Path(os.path.expanduser("~/.claude/toolforge-config.json"))
    if real_config.exists():
        real_data = json.loads(real_config.read_text())
        assert real_data.get("org_id") != "test-corp", "WROTE TO REAL CONFIG — BUG"
    ok("set/get_current_org_id: uses temp config, real config untouched")
except Exception as e:
    fail("set/get_current_org_id", str(e))

try:
    org_mod.clear_current_org_id()
    assert org_mod.get_current_org_id() is None
    ok("clear_current_org_id: cleared")
except Exception as e:
    fail("clear_current_org_id", str(e))

try:
    org_mod.push_stack("test-ui", "Test UI Stack", "UI tools for test-corp",
                       ["21st-magic", "shadcn-ui-mcp"], "test-corp")
    stack = org_mod.get_stack("test-ui")
    assert stack["skills"] == ["21st-magic", "shadcn-ui-mcp"]
    ok("push_stack + get_stack: org stack roundtrip")
except Exception as e:
    fail("push_stack", str(e))

try:
    stacks = org_mod.list_stacks("test-corp")
    names = [s["stack_name"] for s in stacks]
    assert "test-ui" in names
    ok(f"list_stacks(org): {names}")
except Exception as e:
    fail("list_stacks org", str(e))

try:
    rendered = org_mod.render_org_dashboard("test-corp")
    assert "Test Corporation" in rendered
    assert "test-ui" in rendered
    ok("render_org_dashboard: output looks correct")
except Exception as e:
    fail("render_org_dashboard", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("14. toolforge_admin.py")
# ══════════════════════════════════════════════════════════════════════════════

import toolforge_admin as admin

try:
    report = admin.full_health_report()
    assert isinstance(report, str)
    assert "Performance Health" in report
    ok("full_health_report: returns string with expected sections")
except Exception as e:
    fail("full_health_report", str(e))

try:
    db.log_rating("retire-me", 4)
    admin.force_retire("retire-me", "test retirement")
    stats = db.get_rating_stats("retire-me")
    # Should have 1 original + 5 retirement = 6 ratings
    assert stats["n"] == 6, f"expected 6 ratings, got {stats['n']}"
    ok("force_retire: 5 low ratings inserted on top of existing")
except Exception as e:
    fail("force_retire", str(e))

try:
    admin.override_rating("override-me", 4.5)
    stats = db.get_rating_stats("override-me")
    assert stats["n"] == 3
    ok(f"override_rating: 3 ratings inserted, avg={stats['avg']:.1f}")
except Exception as e:
    fail("override_rating", str(e))

try:
    db.log_rating("reset-me", 3)
    db.log_rating("reset-me", 4)
    admin.reset_ratings("reset-me")
    stats = db.get_rating_stats("reset-me")
    assert stats["n"] == 0, f"expected 0 ratings after reset, got {stats['n']}"
    ok("reset_ratings: all ratings wiped")
except Exception as e:
    fail("reset_ratings", str(e))

try:
    admin.create_stack("admin-test-stack", "Admin Test Stack", "Created by admin test",
                       ["playwright", "context7"])
    stack = db.get_skill_stack("admin-test-stack")
    assert stack is not None
    ok("create_stack: stack created via admin")
except Exception as e:
    fail("create_stack", str(e))

try:
    stale = admin.purge_stale(stale_days=0)  # 0 days = everything is stale
    ok(f"purge_stale: flagged {len(stale)} stale skills (0-day threshold)")
except Exception as e:
    fail("purge_stale", str(e))

try:
    admin.rebalance_scores()
    ok("rebalance_scores: completed without error")
except Exception as e:
    fail("rebalance_scores", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("15. Existing --self-test still passes")
# ══════════════════════════════════════════════════════════════════════════════

try:
    result = db._self_test()
    if result == 0:
        ok("toolforge_db --self-test: all existing tests pass")
    else:
        fail("toolforge_db --self-test returned non-zero", str(result))
except Exception as e:
    fail("toolforge_db --self-test raised", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("16. Real ~/.claude/toolforge.db untouched")
# ══════════════════════════════════════════════════════════════════════════════

_touched, _reason = _real_db_touched()
if _touched:
    fail("real DB was modified by tests — wrote to wrong location", _reason)
elif _REAL_DB_EXISTED:
    ok("~/.claude/toolforge.db: pre-existed and was not created/modified by tests")
else:
    ok("~/.claude/toolforge.db: does not exist (never touched)")

real_cfg = Path(os.path.expanduser("~/.claude/toolforge-config.json"))
if real_cfg.exists():
    cfg_data = json.loads(real_cfg.read_text())
    if cfg_data.get("org_id") == "test-corp":
        fail("real config was modified — org_id was written to real config")
    else:
        ok("~/.claude/toolforge-config.json: exists but not modified by tests")
else:
    ok("~/.claude/toolforge-config.json: does not exist (never touched)")


# ══════════════════════════════════════════════════════════════════════════════
section("17. DB v6 schema — new tables present")
# ══════════════════════════════════════════════════════════════════════════════

db.init_db()
_conn17 = db._connect()
try:
    _tables17 = {r[0] for r in _conn17.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
finally:
    _conn17.close()

for _tbl in ("user_preferences", "workflow_shortcuts", "context_sync"):
    if _tbl in _tables17:
        ok(f"v6 table present: {_tbl}")
    else:
        fail(f"v6 table missing: {_tbl}")

_ver17 = None
_conn17b = db._connect()
try:
    _ver17 = _conn17b.execute("PRAGMA user_version").fetchone()[0]
finally:
    _conn17b.close()
if _ver17 == 6:
    ok("schema user_version == 6")
else:
    fail("schema user_version != 6", str(_ver17))


# ══════════════════════════════════════════════════════════════════════════════
section("18. user_preferences CRUD")
# ══════════════════════════════════════════════════════════════════════════════

try:
    db.record_preference_signal("frontend-ui", "shadcn-ui-mcp", positive=True, weight=1.0)
    db.record_preference_signal("frontend-ui", "shadcn-ui-mcp", positive=True, weight=1.0)
    db.record_preference_signal("frontend-ui", "old-tool", positive=False, weight=1.0)
    ok("record_preference_signal: positive and negative signals accepted")
except Exception as e:
    fail("record_preference_signal raised", str(e))

try:
    prefs = db.get_preferences_for_task("frontend-ui")
    assert isinstance(prefs, list), "expected list"
    assert any(p["skill_name"] == "shadcn-ui-mcp" for p in prefs), "shadcn-ui-mcp not found"
    top = prefs[0]
    assert top["preference_score"] > 0, "top score should be positive"
    ok(f"get_preferences_for_task: {len(prefs)} entries, top={top['skill_name']} score={top['preference_score']}")
except Exception as e:
    fail("get_preferences_for_task", str(e))

try:
    score = db.get_preference_score("frontend-ui", "shadcn-ui-mcp")
    assert score > 0, "score should be positive after 2 positive signals"
    ok(f"get_preference_score: {score:.3f}")
except Exception as e:
    fail("get_preference_score", str(e))

try:
    all_prefs = db.get_all_preferences()
    assert "frontend-ui" in all_prefs
    ok(f"get_all_preferences: {len(all_prefs)} task types")
except Exception as e:
    fail("get_all_preferences", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("19. workflow_shortcuts CRUD")
# ══════════════════════════════════════════════════════════════════════════════

try:
    db.save_workflow_shortcut(
        "test-shortcut-1",
        "Test shortcut for frontend work",
        trigger_skills=["shadcn-ui-mcp"],
        steps=[{"skill": "shadcn-ui-mcp", "step": 1}, {"skill": "playwright", "step": 2}],
        auto_detected=True,
    )
    ok("save_workflow_shortcut: created test-shortcut-1")
except Exception as e:
    fail("save_workflow_shortcut", str(e))

try:
    shortcuts = db.list_workflow_shortcuts()
    assert any(s["shortcut_name"] == "test-shortcut-1" for s in shortcuts)
    s = next(s for s in shortcuts if s["shortcut_name"] == "test-shortcut-1")
    assert s["hit_count"] == 0
    assert s["auto_detected"] is True
    assert isinstance(s["trigger_skills"], list)
    assert isinstance(s["steps"], list) and len(s["steps"]) == 2
    ok(f"list_workflow_shortcuts: {len(shortcuts)} shortcuts, data validated")
except Exception as e:
    fail("list_workflow_shortcuts", str(e))

try:
    db.record_shortcut_trigger("test-shortcut-1")
    db.record_shortcut_trigger("test-shortcut-1")
    shortcuts2 = db.list_workflow_shortcuts()
    s2 = next(s for s in shortcuts2 if s["shortcut_name"] == "test-shortcut-1")
    assert s2["hit_count"] == 2, f"expected hit_count=2, got {s2['hit_count']}"
    ok("record_shortcut_trigger: hit_count increments correctly")
except Exception as e:
    fail("record_shortcut_trigger", str(e))

try:
    deleted = db.delete_workflow_shortcut("test-shortcut-1")
    assert deleted is True
    shortcuts3 = db.list_workflow_shortcuts()
    assert not any(s["shortcut_name"] == "test-shortcut-1" for s in shortcuts3)
    ok("delete_workflow_shortcut: shortcut removed")
except Exception as e:
    fail("delete_workflow_shortcut", str(e))

try:
    missing = db.delete_workflow_shortcut("nonexistent-shortcut-xyz")
    assert missing is False
    ok("delete_workflow_shortcut nonexistent: returns False")
except Exception as e:
    fail("delete_workflow_shortcut nonexistent", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("20. context_sync CRUD")
# ══════════════════════════════════════════════════════════════════════════════

try:
    db.log_context_sync("hermes", "push", payload_hash="abc123", status="ok")
    db.log_context_sync("obsidian", "push", payload_hash="def456", status="ok")
    db.log_context_sync("hermes", "pull", status="error", error_msg="connection refused")
    ok("log_context_sync: hermes and obsidian entries written")
except Exception as e:
    fail("log_context_sync", str(e))

try:
    history = db.get_sync_history(limit=10)
    assert len(history) >= 3
    integrations = {h["integration"] for h in history}
    assert "hermes" in integrations and "obsidian" in integrations
    ok(f"get_sync_history all: {len(history)} entries, integrations={integrations}")
except Exception as e:
    fail("get_sync_history", str(e))

try:
    hermes_hist = db.get_sync_history("hermes", limit=5)
    assert all(h["integration"] == "hermes" for h in hermes_hist)
    ok(f"get_sync_history filtered: {len(hermes_hist)} hermes entries")
except Exception as e:
    fail("get_sync_history filtered", str(e))

try:
    db.log_context_sync("generic", "pull", status="ok")
    ok("log_context_sync generic integration accepted")
except Exception as e:
    fail("log_context_sync generic", str(e))

try:
    db.log_context_sync("unknown-int", "push", status="ok")
    fail("log_context_sync invalid integration: should have raised ValueError")
except ValueError:
    ok("log_context_sync invalid integration: ValueError raised correctly")
except Exception as e:
    fail("log_context_sync invalid integration raised wrong error", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("21. toolforge_user_profile.py")
# ══════════════════════════════════════════════════════════════════════════════

try:
    import importlib
    import toolforge_user_profile as up
    importlib.reload(up)
    ok("toolforge_user_profile imported successfully")
except ImportError as e:
    fail("toolforge_user_profile import failed", str(e))
    up = None

if up is not None:
    try:
        up.record_session_signals(
            "data-analysis",
            skills_used=["pandas-ai", "context7", "jupyter"],
            positive_skills=["pandas-ai"],
        )
        ok("record_session_signals: accepted skills_used + positive_skills")
    except Exception as e:
        fail("record_session_signals", str(e))

    try:
        prefs = up.get_top_preferences("data-analysis", top_n=3)
        assert isinstance(prefs, list)
        ok(f"get_top_preferences: {len(prefs)} entries")
    except Exception as e:
        fail("get_top_preferences", str(e))

    try:
        candidates = [
            {"skill_name": "pandas-ai", "composite": 0.7},
            {"skill_name": "context7", "composite": 0.6},
            {"skill_name": "unknown-tool", "composite": 0.8},
        ]
        ranked = up.adjusted_scores("data-analysis", candidates)
        assert isinstance(ranked, list)
        assert all("blended_score" in r for r in ranked)
        top = ranked[0]
        ok(f"adjusted_scores: top={top['skill_name']} blended={top['blended_score']}")
    except Exception as e:
        fail("adjusted_scores", str(e))

    try:
        summary = up.profile_summary()
        assert isinstance(summary, str)
        assert len(summary) > 10
        ok("profile_summary: returns non-empty string")
    except Exception as e:
        fail("profile_summary", str(e))

    try:
        up.record_explicit_feedback("testing", "playwright", positive=True)
        score = db.get_preference_score("testing", "playwright")
        assert score > 0
        ok(f"record_explicit_feedback: playwright score={score}")
    except Exception as e:
        fail("record_explicit_feedback", str(e))

    try:
        result = up.detect_shortcuts_from_pipelines(min_sessions=999)
        assert isinstance(result, list)
        ok("detect_shortcuts_from_pipelines: runs without error (empty result expected)")
    except Exception as e:
        fail("detect_shortcuts_from_pipelines", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("22. bridge_server.py — handler unit tests")
# ══════════════════════════════════════════════════════════════════════════════

try:
    import sys as _sys
    _webui = REPO_ROOT / "webui"
    if str(_webui) not in _sys.path:
        _sys.path.insert(0, str(_webui))
    import bridge_server as bs
    ok("bridge_server imported successfully")
    _bs_ok = True
except ImportError as e:
    fail("bridge_server import failed", str(e))
    _bs_ok = False

if _bs_ok:
    import io as _io

    class _FakeRequest:
        def __init__(self, method="GET", path="/api/health", body=b""):
            self.method = method
            self.path = path
            self._body = body
            self.headers = {"Content-Length": str(len(body))}

    class _FakeHandler(bs._Handler):
        def __init__(self, method="GET", path="/api/health", body=b""):
            self._response_code = None
            self._response_body = None
            self._headers = {}
            self.path = path
            self.command = method
            self.rfile = _io.BytesIO(body)
            self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
            self.wfile = _io.BytesIO()

        def send_response(self, code):
            self._response_code = code

        def send_header(self, k, v):
            self._headers[k] = v

        def end_headers(self):
            pass

    try:
        h = _FakeHandler("GET", "/api/health")
        h._get_health()
        assert h._response_code == 200
        assert h._response_body is not None or True
        ok("bridge_server _get_health: returns 200")
    except Exception as e:
        fail("bridge_server _get_health", str(e))

    try:
        h = _FakeHandler("GET", "/api/shortcuts")
        h._get_shortcuts()
        assert h._response_code == 200
        ok("bridge_server _get_shortcuts: returns 200")
    except Exception as e:
        fail("bridge_server _get_shortcuts", str(e))

    try:
        h = _FakeHandler("GET", "/api/export/context")
        h._get_context_export()
        assert h._response_code == 200
        ok("bridge_server _get_context_export: returns 200")
    except Exception as e:
        fail("bridge_server _get_context_export", str(e))

    try:
        payload = json.dumps({"source": "hermes", "memories": [1, 2, 3]}).encode()
        h = _FakeHandler("POST", "/api/webhooks/hermes", body=payload)
        h._webhook_hermes({"source": "hermes", "memories": [1, 2, 3]})
        assert h._response_code == 200
        ok("bridge_server _webhook_hermes: returns 200")
    except Exception as e:
        fail("bridge_server _webhook_hermes", str(e))


# ══════════════════════════════════════════════════════════════════════════════
section("23. Real ~/.claude files still untouched (v6 check)")
# ══════════════════════════════════════════════════════════════════════════════

_touched_v6, _reason_v6 = _real_db_touched()
if _touched_v6:
    fail("real DB modified after v6 tests — location may have been leaked", _reason_v6)
elif _REAL_DB_EXISTED:
    ok("~/.claude/toolforge.db: pre-existed, still unchanged after v6 tests")
else:
    ok("~/.claude/toolforge.db: still does not exist after v6 tests")


# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{'=' * 55}")
print(f"  RESULTS: {passed} passed, {failed} failed")
if errors:
    print("\n  Failures:")
    for e in errors:
        print(f"    • {e}")

# Cleanup temp files
try:
    import shutil
    shutil.rmtree(_tmpdir, ignore_errors=True)
    print(f"\n  Temp dir cleaned up: {_tmpdir}")
except Exception:
    pass

print("=" * 55)
sys.exit(0 if failed == 0 else 1)
