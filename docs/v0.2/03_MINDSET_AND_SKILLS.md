# Mindset & Skills Playbook

How to think about each phase + which Claude skill/agent to invoke at which juncture. Maps the global `~/.claude/CLAUDE.md` Skill Selection Loop to ToolForge-specific work.

---

## Meta-mindset (read first)

### The router is not a feature — it's a hypothesis

The whole v0.2 is a one-line bet: **if Claude sees a ranked shortlist of installed skills BEFORE picking a tool, it will pick a skill more often than 0.2% of the time.** Everything downstream — verify, rate-infer, README rewrite — only matters if that bet pays off. Phase B (router build) is the gate; everything else is leverage on a confirmed win.

This means:

- **Don't over-build before the bet is tested.** Threshold tuning (Phase B step 7) and shadow-mode (Phase B step 5) are the actual experiments. Schedule a checkpoint after both. If shadow false-positive rate is >25% and the hand-label exercise (Phase A.1a) revealed <8/20 missed triggers, the bet has lost and you reframe — not iterate router for another week.
- **Empirical > theoretical.** Day-1 dumb scanner gave you 79%. The pressure-test critique flagged this as an upper bound. The hand-label exercise + shadow mode are how you convert the upper bound into a real number. Every downstream number (demo claims, README percentages) must trace to one of these two sources.
- **Council's killed list is load-bearing.** Cloud sync, accounts, telemetry-home, gap detector, routing-everything-not-just-skills, webui sliders — these will keep tempting you. Re-read the anti-vision in `01_END_PRODUCT.md` weekly.
- **`[CRITIQUE]` Shadow before active.** This is the new mindset shift after pressure-test: ship the router in log-only mode for 7 days. Get a real measured false-positive rate. THEN promote to active. Skipping shadow = shipping vibes.

### Anti-patterns to dodge (specific to this project)

| Anti-pattern | Why it's tempting | Why it's wrong |
|--------------|-------------------|----------------|
| Add MORE keywords to category sets | Easy refactor, feels productive | Keywords are pre-tf-idf shorthand. Once router lands, real corpus matching replaces them. Don't tune what you're about to replace. |
| Polish the webui flow studio | Visible, immediate, dopamine | webui is v0.1 sidecar. v0.2 is invisible (hook-driven). Polish webui in v0.3. |
| Write more tests for already-tested paths | Felt "thorough" | usage_detector has 17 contract tests + 10 self-tests. That's enough. Spend tests on router corpus matching, not classifier branches. |
| Refactor `local_scan.py` into `inventory.py` now | "Tech debt" | DEFERRED. User has WIP on `commands/toolforge.md` that references local_scan. Touching it now = merge conflict. Defer to v0.2 final cleanup or v0.3. |
| Build the audit panel before the router lands | Plan v1 thinking | Plan v2 explicitly demoted audit. Router-first. Audit derives from router data once it exists. |
| Add a "share my ratings" button | Network effects! | Killed. Privacy-local is the moat. |
| **`[CRITIQUE]` Route MCPs/agents/commands alongside skills** | "more coverage = better" | NO. MCPs are passive pools; commands have `/slash`; agents spawn explicitly. Routing them is noise. Skills only in v0.2. |
| **`[CRITIQUE]` Trust the 79% dead-rate as ground truth** | Already validated empirically! | NO. It's an upper bound. 79% counts skills you legitimately don't need on a given day. Hand-label first. |
| **`[CRITIQUE]` Skip the inventory cache** | "build_inventory works, just call it" | NO. Per-call `git log` subprocess fan-out kills hook latency. Cache FIRST or the hook is DOA. |
| **`[CRITIQUE]` Ship `router_mode: active` as v0.2.0 default** | "shadow mode is extra work" | NO. Shadow → measure → tune → activate is the sequence. Active-by-default means shipping vibes-based threshold. |

---

## Skill invocation map — by phase

For each phase, the **MUST invoke** skills are non-optional gates (Gate 1 from CLAUDE.md). The **SHOULD invoke** are situational. The **MAY invoke** are if scope balloons.

### Phase A — Demand validation (user task, no code)

**Mindset**: this is a kill-the-product-fast checkpoint. If 2/3 don't say yes, the entire v0.2 router thesis is unproven. Better to know now than after 25 focused hours.

| Skill | Use when |
|-------|----------|
| **MUST** none — this is conversational, not code. | — |
| **SHOULD** `gstack-office-hours` | If you want a thinking partner to help frame the 2 questions to maximize signal. Useful if you're worried about leading the witness. |
| **MAY** `llm-council` | If the 3 power-user responses split (1 yes, 1 no, 1 maybe) and you want to pressure-test the next decision. |

**Output of phase A**: a one-paragraph note in `~/.gstack/projects/Tool-Forge/` saying "3/3 said yes" or "2/3 said yes" or "1/3 said yes — reframing to X". Goes into the v0.2 README justification.

### Phase B — Router build

**Mindset**: this is the highest-risk phase. Two unknowns stacked: (1) is the tf-idf signal strong enough to discriminate? (2) does Anthropic's UserPromptSubmit hook contract match what you expect? Validate (2) BEFORE writing (1).

| Step | Skill | Why |
|------|-------|-----|
| Before any code | **MUST** `claude-code-guide` agent OR context7 `MCP query-docs` | Fetch the actual UserPromptSubmit hook spec. Get exit-code semantics, stdin shape, stdout injection format right ONCE — wrong format = silent no-op. |
| Before any code | **MUST** `gstack-plan-eng-review` | Pressure-test the router design (tf-idf vs embeddings vs LLM scoring) before writing 250 lines. Council already weighed in at design level; this is the engineering-detail check. |
| Mid-build | **SHOULD** `feature-dev:code-architect` (Agent) | If the corpus-build / cache-invalidation logic gets gnarly, spawn one agent to draft, you review. |
| After code, before merge | **MUST** `pr-review-toolkit:silent-failure-hunter` | The hook MUST fail silent on timeout / parse error — but not silent on bugs. This skill catches the difference. |
| After code, before merge | **MUST** `pr-review-toolkit:code-reviewer` | Style + invariants + style-guide adherence. |
| Threshold tuning | **MUST** `gstack-investigate` mindset | You're doing a measurement, not building. Apply the investigate workflow: form hypothesis, gather data, draw conclusion, document. |

**Skip these for this phase**: design skills (impeccable, taste-skill, etc.) — there's no UI in the router. No GSAP. No 21st.dev. Skip them all.

### Phase C — Verify + Rate

**Mindset**: these are mechanical features. Highest risk = the schema v3 migration (`ratings.source` + `weight` column add). Schema bumps must be backwards-compatible — anyone running v0.1 must upgrade cleanly. Tested precedent: v1→v2 migration in `4883880` is the template; follow it line-by-line.

| Step | Skill | Why |
|------|-------|-----|
| Schema v3 bump | **MUST** `gstack-investigate` style first | Run `python toolforge/bin/toolforge_db.py schema_version` on a fresh DB AND an upgraded one. Confirm no data loss path. |
| Schema v3 bump | **MUST** read `bin/toolforge_db.py:init_db` first | Pattern-match the v1→v2 migration. Same `PRAGMA user_version` dance. |
| verify_install build | **MUST** `pr-review-toolkit:silent-failure-hunter` | This script's whole purpose is catching silent install failures. If verify_install itself fails silent, recursion of failure. |
| rate_inferrer build | **MUST** `pr-review-toolkit:code-reviewer` | Likert inference rule is opinionated. Reviewer flags hidden assumptions. |
| rate_inferrer build | **SHOULD** `pr-review-toolkit:type-design-analyzer` | The new `source` enum (`user`, `inferred`, future-`community`) is a design choice. Type-design check makes sure the enum can extend without breaking. |
| Post-build | **MUST** add JSONL-style **contract test** for `claude mcp list` output format | New external CLI dependency. Same pattern as `tests/test_usage_detector_contract.py`. |

### Phase D — README + ship

**Mindset**: README is the marketing surface. Most readers read 50 words before deciding install/skip. Spend disproportionate effort on the first paragraph.

| Step | Skill | Why |
|------|-------|-----|
| README hero rewrite | **MUST** `taste-skill` | Anti-slop landing page rules apply to README hero too. Avoid AI-cliché openers ("In a world of..."). Specific numbers (79% → 5%) beat adjectives. |
| README hero rewrite | **SHOULD** `gstack-design-shotgun` | 3 hero variants in parallel; pick the strongest. Cheap insurance against the first draft being mid. |
| CHANGELOG entry | **MUST** read `CHANGELOG.md` first | Match existing voice. The v0.1 entry is the template; don't reinvent format. |
| Pre-tag | **MUST** `gstack-qa` | Full QA pass — run every self-test, confirm clean install in a fresh `~/.claude` dir, dry-run the upgrade path. |
| Pre-tag | **SHOULD** `gstack-review` | Final cross-file diff review. |
| Tag + push | **MUST** `gstack-ship` workflow | Don't manually compose the release ceremony. Use the skill. |

---

## Decision framework — when uncertain

### When facing a multi-option call

1. State the options + the success metric they'd move.
2. If the metric is M1 (skill-fire rate), pick the option that moves it most. Use Day-1 data as the anchor.
3. If the metric is M3 (false-positive rate) and the option is opaque (e.g. "tune threshold to 0.5 vs 0.7"), DON'T pick — run the threshold-tuning experiment first.
4. If 2+ options pass the metric, invoke `llm-council`. 5 advisors + peer review + chairman in ~6 min beats 30 min of solo deliberation.
5. If the call is reversible (single commit, no schema bump), default to ACT. If irreversible (schema bump, public API), default to PAUSE + council.

### When stuck on a bug

1. `gstack-investigate` skill — ALWAYS, no ad-hoc debugging.
2. If 30 min in with no progress, spawn `general-purpose` Agent with the specific question + relevant code paths.
3. If 60 min in, call `advisor()` — full conversation goes to a stronger reviewer.

### When considering a refactor

1. Is it on the v0.2 critical path? If NO → don't refactor. Add to v0.3 backlog.
2. Will it cause merge conflict with user's WIP? If YES → defer.
3. Will it block another feature? If NO → defer.
4. If yes-to-1, no-to-2, yes-to-3 → refactor, but commit it separately from the feature that needs it.

---

## Specific skill cheat sheet for ToolForge-shaped tasks

| Task shape | Default skill | Fallback |
|------------|---------------|----------|
| "Add a Python script to `bin/`" | `pr-review-toolkit:code-reviewer` after write | `code-simplifier` for refinement |
| "Modify a hook" | `claude-code-guide` for spec verification | `pr-review-toolkit:silent-failure-hunter` post-write |
| "Schema bump" | Read `bin/toolforge_db.py:init_db` for the v1→v2 template | `gstack-investigate` to verify migration safety |
| "New CLI command" | Match existing `--self-test` + `--help` patterns | None — this is mechanical |
| "Write contract tests" | Pattern-match `tests/test_usage_detector_contract.py` | None — pattern is established |
| "README/CHANGELOG edit" | `taste-skill` for hero, mirror existing voice for body | `gstack-design-shotgun` for alternatives |
| "UI work in webui" | DEFER to v0.3. If unavoidable: `impeccable` for polish, `21st.dev` for components | `frontend-design` plugin |
| "Threshold tuning / measurement" | `gstack-investigate` workflow | None |
| "Strategic scope decision" | `llm-council` | `gstack-plan-ceo-review` |
| "Engineering design decision" | `gstack-plan-eng-review` | `feature-dev:code-architect` Agent |
| "Bug" | `gstack-investigate`, ALWAYS | `advisor()` if stuck |

---

## Mandatory pre-write skill announcement

From the global `~/.claude/CLAUDE.md` Hard Gates section — **before any state-mutating tool call**:

> First sentence of every non-trivial response must list 2-3 skill candidates, score each 1-5, name the pick. Format: `Candidates: <skill-A> (score N, reason), <skill-B> (score N, reason). Chose <X>.` OR `Candidates: <A,B,C> all ≤2 — direct exec because <reason>.`

For ToolForge work specifically, candidate lists should include at LEAST one of: `gstack-investigate`, `pr-review-toolkit:silent-failure-hunter`, `feature-dev:code-architect`, `llm-council`. If none fit → state direct exec explicitly.

---

## When in doubt

- **Don't write code without a skill.** Anti-pattern #1 from the global CLAUDE.md.
- **Don't merge without `gstack-qa`.** Self-tests aren't QA — they're unit checks.
- **Don't ship without `gstack-ship` workflow.** Release ceremony is too error-prone for ad-hoc.
- **Don't refactor unless on critical path.** v0.2 is small and focused. Polish in v0.3.
- **Don't ignore a council finding.** If 4/5 advisors flag the same risk, that's signal not noise.
