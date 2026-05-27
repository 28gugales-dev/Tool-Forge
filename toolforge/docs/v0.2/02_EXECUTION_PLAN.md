# Execution Plan — v0.2

State as of 2026-05-25, after 5 commits landed this session. See `git log` for hash receipts.

---

## Status board

### ✅ Done (commit hash → what)

| Commit | What | Verify |
|--------|------|--------|
| `f98c607` | v0.1 initial release (curator skill, installer, ratings, fallback, demo, webui) | `git tag` shows v0.1.0 |
| `087d6f3` | Local-source scanner + curator v0.3.0 bump | `bin/toolforge_local_scan.py --self-test` |
| `99f5cb4` | **Day-1 scanner — validated 79% dead-rate thesis empirically** (superseded by `usage_detector`) | Re-run: `python toolforge/bin/toolforge_usage_detector.py --days 30` |
| `4883880` | DB schema v2 (usage_stats + deprecations + routing_scores) + helpers | `python toolforge/bin/toolforge_db.py --self-test` → 9/9 |
| `bd02c44` | Productionized usage_detector with DB persist + caps | `python toolforge/bin/toolforge_usage_detector.py --self-test` → 10/10 |
| `c29adb4` | Inventory curator port + JSONL contract tests (17 passing) | `python toolforge/tests/test_usage_detector_contract.py` → 17/17 |
| `7448c12` | Half-life 180d → 75d (AI tooling moves fast) | grep `DECAY_HALFLIFE_DAYS` |

### 🔴 Pending v0.2 (in dependency order — DO NOT skip ahead)

#### Phase A — Validation gate (BLOCKER for Phase B)

1. **[ ] Demand validation** (user task, ~1 day) — ask 3 power CC users two questions:
   - "Of your installed skills, what % do you think Claude actually invokes vs. lets sit dead?"
   - "If a hook silently told Claude 'here are 3 matching skills' before each prompt, would you turn it on?"
   - **GATE**: if <2/3 say yes → reframe before building router. The 79% dead-rate is YOUR data; this confirms OTHERS feel it too.

1a. **[ ] `[CRITIQUE]` 20-prompt hand-label exercise** (~3 hrs, BLOCKER for Phase B)
   - Pull 20 random user prompts from your own `~/.claude/projects/*.jsonl`.
   - For each: label "should an installed skill have fired here? yes / no / maybe".
   - Tally yes count.
   - **GATE**:
     - ≥12/20 yes → router thesis confirmed, proceed with confidence
     - 8-11/20 → marginal, build router but tighten expectations + monitor M1 hard
     - <8/20 → STOP. Run repair playbook in `04_RISKS_AND_MITIGATIONS.md` R1.1. The 79% dead-rate is an upper bound that includes skills you legitimately don't need on a given day. Without this gate, the v0.2 router is built on potentially-30% pain not 79% pain.
   - Output: short note in `~/.gstack/projects/Tool-Forge/handlabel-results-YYYYMMDD.md` with the 20 rows + tally. Goes into v0.2 README + demo as the methodology anchor.

#### Phase B — Router build (~8-10 hours focused, was 6-8 — see critique adds)

2. **[ ] `[CRITIQUE]` Add inventory disk cache FIRST** (~1 hour) — BEFORE router work.
   - `webui/inventory.py:build_inventory()` currently shells `git log` per item — tens of synchronous subprocesses per call. Cannot fire on every UserPromptSubmit.
   - Add `tempdir/toolforge_inventory.json`, 1hr TTL.
   - Invalidate on mtime change of: `~/.claude/skills/`, `~/.claude/plugins/installed_plugins.json`, `~/.claude.json`.
   - Forced invalidation: `bin/toolforge_install.py` deletes cache after every successful install.
   - Self-test: warm cache hit must be <10ms; cold rebuild <500ms.

3. **[ ] `bin/toolforge_router.py`** (~250 lines + 80 test) — tf-idf scoring engine.
   - Build corpus from cached `build_inventory()` output (name + description).
   - **`[CRITIQUE]` Scope: skills ONLY in v0.2.** Not MCPs, plugins, agents, or slash commands. Routing those produces noise (MCPs are passive connection pools; commands have own invocation surface). One clean surface = one clean metric. Add others in v0.3 once skill routing is empirically tuned.
   - Score user prompt against corpus, return top-K above threshold.
   - Cache TF-IDF index to `tempdir/toolforge_router_idx.json`, 1hr TTL, invalidate on inventory cache change.
   - **Stop-words list** must include programming verbs ("write", "add", "fix") to prevent everything matching.
   - **Self-test**: precision/recall against a hand-labeled fixture of 10 prompts → 10 expected skills.
   - **`[CRITIQUE]` Saturation cap**: `usage_boost = min(0.5, count_30d / 100)`. Prevents one heavily-used skill from dominating the composite (see R1.3 self-poisoning).

4. **[ ] `hooks/user-prompt-router.py`** (~150 lines + 60 test) — UserPromptSubmit hook.
   - **Read Anthropic UserPromptSubmit hook spec first** via context7 or `~/.claude/docs/`. Wrong output format = silent no-op.
   - Wall-clock budget: **80ms hard** (was 100ms — tightened per R1.2). Return empty on timeout.
   - Inject as `<system-reminder>` so Claude treats as context not user content.
   - **Strip embedded `<system-reminder>` tags + cap each description to 100 chars + cap total injection to 500 chars** (prompt-injection echo defense, R2.1).
   - **Charset filter** description fields to `[A-Za-z0-9 ._,;:!?()-/]` before injection.
   - Add to `plugin.json` hooks block.
   - Contract test: feed canonical prompt shapes through, assert injection format stable.
   - **Logging**: timer per call, dumped to `~/.claude/toolforge_router.log` for latency tracking.

5. **[ ] `[CRITIQUE]` Shadow mode FIRST — ship router in log-only mode for 7 days**
   - Config flag `router_mode: shadow|active` in `~/.claude/toolforge-config.json`, **default `shadow`** for v0.2.0.
   - Shadow mode: router runs full scoring + would-be-injection, logs `{prompt_hash, top_3_keys, top_3_scores, would_inject: bool}` to `~/.claude/toolforge_router.log`. Injects nothing into Claude's context.
   - **GATE before flipping to active**: 7 days of shadow data + manual review of 50 logged decisions. False-positive rate (would-have-fired-incorrectly) must be <15%. If not → tune threshold or expand stop-words before promoting.
   - This is the source of demo metrics (R2.2). Demo claim "router would have fired on X%" must trace to a logged number, not vibes.

6. **[ ] `[CRITIQUE]` Attribution split for usage_boost** — schema work for R1.3.
   - Add `router_attributed BOOLEAN DEFAULT FALSE` column to `usage_stats` (schema v3 migration along with rate_inferrer's columns).
   - Track invocations originating from router-suggested skills separately. Use ONLY non-router-attributed counts for `usage_boost`.
   - Without this: feedback loop. First skill across threshold compounds dominance.

7. **[ ] Threshold tuning session** (~1 hour) — once shadow data exists:
   - Pull last 7d shadow log.
   - For each top-1 suggestion, manually label: would this injection have helped, hurt, or been neutral?
   - Tune threshold to keep false-positive rate <10%. Document final number in plan v3 commit.
   - **Diversification floor**: top-K must span ≥2 distinct skill categories when possible.

#### Phase C — Verify + Rate (~4-5 hours focused)

5. **[ ] `bin/toolforge_verify_install.py`** (~100 lines + 60 test).
   - Args: `<tool_name> <type>` where type ∈ {plugin, mcp, skill}.
   - Plugin: `claude plugin list` → grep name.
   - MCP: `claude mcp list` → grep name → optional `tools/list` smoke (if --smoke flag).
   - Skill: stat `~/.claude/skills/<name>/SKILL.md` exists.
   - **Idempotent**: re-run produces same result.
   - DB: writes `verified_at` to `installs` table (need to add column — schema bump v3 OR live in `usage_stats` extra columns).
   - Wire into `bin/toolforge_install.py` post-success path.

6. **[ ] `bin/toolforge_rate_inferrer.py`** (~120 lines + 70 test).
   - Reads `usage_stats`, filters tools w/ `count_30d ≥ 5` AND no user rating in last 30d.
   - Writes `ratings` row w/ `value=4, source='inferred', weight=0.5`.
   - **Idempotent**: re-runs don't accumulate duplicate inferred rows for same tool/day.
   - Schema bump: add `source TEXT DEFAULT 'user'` + `weight REAL DEFAULT 1.0` columns to `ratings` (schema v3 migration).
   - Cron-ready (`--quiet` flag) so user can wire to nightly task scheduler.

#### Phase D — README + ship (~3-4 hours)

7. **[ ] README hero rewrite** — three paragraphs:
   - Lead: "Stop typing slash commands. ToolForge auto-routes the right skill before you finish the prompt."
   - Proof: cite the 79% dead-rate + the 5%-target router lift.
   - One-line install: `claude plugin install toolforge@official`.
   - Move old curator-first prose to "Manual mode" section below.

8. **[ ] CHANGELOG.md v0.2.0 entry** with every commit summarized.

9. **[ ] Tag + push v0.2.0**.

### 🟢 Deferred to v0.3 (DO NOT pull forward)

- Pre-recorded forge demo
- Stack-aware mid-task suggestions
- Auto-deprecate UX
- Marketplace listing
- Hot-reload semantics doc

### ⚫ Killed (NEVER build)

- Cloud sync, telemetry-home, accounts
- Zero-click auto-install
- Community ratings ingestion (in current form)

---

## Dependency graph

```
Demand validation (Phase A)
   │
   ▼
toolforge_router.py ◄──── (corpus reads from existing build_inventory)
   │
   ▼
user-prompt-router.py ──── hook contract via context7
   │
   ▼
Threshold tuning ──── consumes 30d of transcripts
   │
   ▼
verify_install.py ◄──── independent of router; can build parallel
   │
   ▼
rate_inferrer.py ◄──── needs schema v3 (ratings.source + weight)
   │
   ▼
README rewrite + CHANGELOG + tag
```

**Parallelizable**: verify_install + rate_inferrer can build in parallel with router (different files, no shared state beyond schema bump which goes first).

---

## Time + bundling rules

### Commit grouping

Each `[ ]` checkbox above = one commit minimum. Bundle ONLY when files are tightly coupled:

| Bundle? | Yes/No |
|---------|--------|
| router.py + hook + plugin.json registration | YES — hook is dead without the engine; one feature |
| router + verify_install | NO — orthogonal; bundling muddies blame on revert |
| verify_install + schema-v3-bump | NO — schema bumps stand alone (precedent: v1→v2 was its own commit) |
| rate_inferrer + schema-v3-bump | YES IF schema bump is for rate_inferrer only — otherwise bump first |
| README + CHANGELOG | YES — same release ceremony |

### Bundling 3 features in one prompt

User asked: can I do router + verify_install + rate_inferrer in ONE prompt?

**Verdict**: yes, BUT three commits (not one). Build order:

1. Schema v3 bump (ratings.source + weight) — 1st commit.
2. verify_install.py (smallest, low risk, builds confidence) — 2nd commit.
3. rate_inferrer.py (medium, uses verify's read patterns) — 3rd commit.
4. router.py + hook (biggest, biggest unknowns) — 4th commit.

If session token-budget runs thin at step 4, steps 1-3 are durable and shippable independently. router can land in a separate session.

### Time honesty

- Router phase: 6-8 hours focused (NOT 1 hour — the threshold tuning + prompt-injection defense + Anthropic spec verification eat time)
- Verify + Rate phase: 4-5 hours focused
- README + ship: 3-4 hours

**Total v0.2 build time honest** (after critique adds): 16-20 hours focused work + a **7-day shadow window** that runs in background while you do unrelated work. Add 50% for context-switching, testing, debugging → **~30 hours wall-clock spread over 2-3 weeks**.

The original plan v1 said "Week 1 invisible foundation, Week 2 detectors, Week 3 audit + ship". Plan v2 collapsed the detector week into the router week. The week count was always aspirational; the hour count above is honest. After the 2026-05-25 pressure-test critique, four new gates landed (hand-label, inventory cache, shadow mode, attribution split). They add ~5 hours but cut the risk of a router that fails empirically on first ship.

---

## Pre-flight before each phase

Before starting any phase, run these in order:

1. `git status --short` — confirm no orphan WIP (user's `install.py` + `commands/toolforge.md` are the known exceptions).
2. `git pull --ff-only origin main` if remote diverged.
3. Read the relevant phase section above + the matching `03_MINDSET_AND_SKILLS.md` section.
4. `python toolforge/bin/toolforge_db.py --self-test` — confirm baseline DB integrity.
5. **Skill announcement** (Gate 1 from global CLAUDE.md): name 2-3 candidate skills, score, pick, before writing code.

---

## End-of-session checkpoint protocol

Whenever wrapping a session:

1. Commit anything that passes self-test, even if incomplete (better partial than lost).
2. Update this doc's status board with new commit hashes.
3. If a `[ ]` item is partially done, leave the box unchecked but add a sub-bullet noting what's complete.
4. If a new risk surfaced, log it in `04_RISKS_AND_MITIGATIONS.md` BEFORE closing the session.
5. Pick the next session's first task and write it as a single sentence at the top of your todos so you can resume cold.
